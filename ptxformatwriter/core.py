"""
Pure Python parser for Pro Tools PTX session files.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, List, Optional


BITCODE = b"0010111100101011"
ZMARK = 0x5A
ZERO_TICKS = 0xE8D4A51000
MAX_CHANNELS_PER_TRACK = 8


class PTFParseError(ValueError):
    """Raised internally when a malformed file cannot be parsed safely."""


@dataclass
class Wav:
    filename: str = ""
    index: int = 0
    posabsolute: int = 0
    length: int = 0


@dataclass
class MidiEvent:
    pos: int = 0
    length: int = 0
    note: int = 0
    velocity: int = 0


@dataclass
class Region:
    name: str = ""
    index: int = 0
    startpos: int = 0
    sampleoffset: int = 0
    length: int = 0
    wave: Wav = field(default_factory=Wav)
    midi: List[MidiEvent] = field(default_factory=list)


@dataclass
class Track:
    name: str = ""
    index: int = 0
    playlist: int = 0
    reg: Region = field(default_factory=Region)


@dataclass
class Block:
    zmark: int
    block_type: int
    block_size: int
    content_type: int
    offset: int
    child: List["Block"] = field(default_factory=list)


@dataclass
class MidiChunk:
    zero: int
    maxlen: int
    chunk: List[MidiEvent]


@dataclass
class TempoEvent:
    pos: int = 0
    bpm: float = 120.0
    ppq: int = 960000


@dataclass
class MeterEvent:
    pos: int = 0
    numerator: int = 4
    denominator: int = 4
    ordinal: int = 1


@dataclass
class MidiPlacement:
    track_index: int = 0
    track_name: str = ""
    region_index: int = 0
    region_name: str = ""
    chunk_index: int = 0
    startpos: int = 0
    midi: List[MidiEvent] = field(default_factory=list)


def _float32(value: float) -> float:
    return struct.unpack("f", struct.pack("f", float(value)))[0]


def _copy_wav(wav: Wav) -> Wav:
    return Wav(
        filename=wav.filename,
        index=wav.index,
        posabsolute=wav.posabsolute,
        length=wav.length,
    )


def _copy_midi_event(event: MidiEvent) -> MidiEvent:
    return MidiEvent(
        pos=event.pos,
        length=event.length,
        note=event.note,
        velocity=event.velocity,
    )


def _copy_region(region: Region) -> Region:
    return Region(
        name=region.name,
        index=region.index,
        startpos=region.startpos,
        sampleoffset=region.sampleoffset,
        length=region.length,
        wave=_copy_wav(region.wave),
        midi=[_copy_midi_event(event) for event in region.midi],
    )


def _copy_track(track: Track) -> Track:
    return Track(
        name=track.name,
        index=track.index,
        playlist=track.playlist,
        reg=_copy_region(track.reg),
    )


class PTFFormat:
    """Read and parse Pro Tools session files."""

    Wav = Wav
    MidiEvent = MidiEvent
    MidiPlacement = MidiPlacement
    Region = Region
    Track = Track
    Block = Block
    TempoEvent = TempoEvent
    MeterEvent = MeterEvent

    def __init__(self) -> None:
        self._path = ""
        self._ptfunxored = b""
        self._len = 0
        self._sessionrate = 0
        self._version = 0
        self._targetrate = 0
        self._ratefactor = 1.0
        self.is_bigendian = False
        self._audiofiles: List[Wav] = []
        self._regions: List[Region] = []
        self._midiregions: List[Region] = []
        self._tracks: List[Track] = []
        self._miditracks: List[Track] = []
        self._midiplacements: List[MidiPlacement] = []
        self._tempoevents: List[TempoEvent] = []
        self._meterevents: List[MeterEvent] = []
        self.blocks: List[Block] = []

    def cleanup(self) -> None:
        self._ptfunxored = b""
        self._len = 0
        self._sessionrate = 0
        self._version = 0
        self._audiofiles.clear()
        self._regions.clear()
        self._midiregions.clear()
        self._tracks.clear()
        self._miditracks.clear()
        self._midiplacements.clear()
        self._tempoevents.clear()
        self._meterevents.clear()
        self.blocks.clear()

    def load(self, path: str | Path, targetsr: int) -> int:
        """Load a session file.

        Return values match the original C++ library:
        0 success; -1 decrypt/open error; -2 version detection error;
        -3 incompatible version; -4 parser error.
        """

        self.cleanup()
        self._path = str(path)

        if self.unxor(self._path):
            return -1
        if self.parse_version():
            return -2
        if self._version < 5 or self._version > 12:
            return -3

        self._targetrate = int(targetsr)
        try:
            err = self.parse()
        except (IndexError, PTFParseError, struct.error):
            err = -1
        if err:
            return -4
        return 0

    def unxor(self, path: str | Path) -> int:
        """Decrypt a Pro Tools session into memory."""

        try:
            raw = Path(path).read_bytes()
        except OSError:
            return -1

        self._len = len(raw)
        if self._len < 0x14:
            return -1

        out = bytearray(raw)
        xor_type = out[0x12]
        xor_value = out[0x13]

        if xor_type == 0x01:
            xor_delta = self.gen_xor_delta(xor_value, 53, False)
        elif xor_type == 0x05:
            xor_delta = self.gen_xor_delta(xor_value, 11, True)
        else:
            return -1

        xor_key = [(i * xor_delta) & 0xFF for i in range(256)]
        for i in range(0x14, self._len):
            xor_index = (i & 0xFF) if xor_type == 0x01 else ((i >> 12) & 0xFF)
            out[i] ^= xor_key[xor_index]

        self._ptfunxored = bytes(out)
        return 0

    def version(self) -> int:
        return self._version

    def sessionrate(self) -> int:
        return self._sessionrate

    def path(self) -> str:
        return self._path

    def audiofiles(self) -> List[Wav]:
        return self._audiofiles

    def regions(self) -> List[Region]:
        return self._regions

    def midiregions(self) -> List[Region]:
        return self._midiregions

    def tracks(self) -> List[Track]:
        return self._tracks

    def miditracks(self) -> List[Track]:
        return self._miditracks

    def midiplacements(self) -> List[MidiPlacement]:
        return self._midiplacements

    def tempoevents(self) -> List[TempoEvent]:
        return self._tempoevents

    def meterevents(self) -> List[MeterEvent]:
        return self._meterevents

    def unxored_data(self) -> bytes:
        return self._ptfunxored

    def unxored_size(self) -> int:
        return self._len

    def find_track(self, index: int) -> Optional[Track]:
        for track in self._tracks:
            if track.index == index:
                return _copy_track(track)
        return None

    def find_region(self, index: int) -> Optional[Region]:
        for region in self._regions:
            if region.index == index:
                return _copy_region(region)
        return None

    def find_miditrack(self, index: int) -> Optional[Track]:
        for track in self._miditracks:
            if track.index == index:
                return _copy_track(track)
        return None

    def find_midiregion(self, index: int) -> Optional[Region]:
        for region in self._midiregions:
            if region.index == index:
                return _copy_region(region)
        return None

    def find_wav(self, index: int, filename: str = "") -> Optional[Wav]:
        for wav in self._audiofiles:
            if wav.index == index or (filename and wav.filename == filename):
                return _copy_wav(wav)
        return None

    @staticmethod
    def regionexistsin(regions: Iterable[Region], index: int) -> bool:
        return any(region.index == index for region in regions)

    @staticmethod
    def wavexistsin(wavs: Iterable[Wav], index: int) -> bool:
        return any(wav.index == index for wav in wavs)

    @staticmethod
    def gen_xor_delta(xor_value: int, mul: int, negative: bool) -> int:
        for i in range(256):
            if ((i * mul) & 0xFF) == xor_value:
                return (-i) & 0xFF if negative else i
        return 0

    @staticmethod
    def get_content_description(ctype: int) -> str:
        return {
            0x0030: "INFO product and version",
            0x1001: "WAV samplerate, size",
            0x1003: "WAV metadata",
            0x1004: "WAV list full",
            0x1007: "region name, number",
            0x1008: "AUDIO region name, number (v5)",
            0x100B: "AUDIO region list (v5)",
            0x100F: "AUDIO region->track entry",
            0x1011: "AUDIO region->track map entries",
            0x1012: "AUDIO region->track full map",
            0x1014: "AUDIO track name, number",
            0x1015: "AUDIO tracks",
            0x1017: "PLUGIN entry",
            0x1018: "PLUGIN full list",
            0x1021: "I/O channel entry",
            0x1022: "I/O channel list",
            0x1028: "INFO sample rate",
            0x103A: "WAV names",
            0x104F: "AUDIO region->track subentry (v8)",
            0x1050: "AUDIO region->track entry (v8)",
            0x1052: "AUDIO region->track map entries (v8)",
            0x1054: "AUDIO region->track full map (v8)",
            0x1056: "MIDI region->track entry",
            0x1057: "MIDI region->track map entries",
            0x1058: "MIDI region->track full map",
            0x2000: "MIDI events block",
            0x2001: "MIDI region name, number (v5)",
            0x2002: "MIDI regions map (v5)",
            0x2028: "TEMPO map",
            0x2029: "METER map",
            0x2067: "INFO path of session",
            0x2511: "Snaps block",
            0x2519: "MIDI track full list",
            0x251A: "MIDI track name, number",
            0x2523: "COMPOUND region element",
            0x2602: "I/O route",
            0x2603: "I/O routing table",
            0x2628: "COMPOUND region group",
            0x2629: "AUDIO region name, number (v10)",
            0x262A: "AUDIO region list (v10)",
            0x262C: "COMPOUND region full map",
            0x2633: "MIDI regions name, number (v10)",
            0x2634: "MIDI regions map (v10)",
            0x2718: "TEMPO lane",
            0x2719: "METER lane",
            0x271A: "MARKER list",
        }.get(ctype, "UNKNOWN content type")

    def _need(self, pos: int, size: int) -> bytes:
        if pos < 0 or pos + size > self._len:
            raise PTFParseError(f"read outside buffer at {pos:#x}")
        return self._ptfunxored[pos : pos + size]

    def _read(self, pos: int, size: int, bigendian: Optional[bool] = None) -> int:
        if bigendian is None:
            bigendian = self.is_bigendian
        return int.from_bytes(self._need(pos, size), "big" if bigendian else "little")

    def _read2(self, pos: int, bigendian: Optional[bool] = None) -> int:
        return self._read(pos, 2, bigendian)

    def _read3(self, pos: int, bigendian: Optional[bool] = None) -> int:
        return self._read(pos, 3, bigendian)

    def _read4(self, pos: int, bigendian: Optional[bool] = None) -> int:
        return self._read(pos, 4, bigendian)

    def _read5(self, pos: int, bigendian: Optional[bool] = None) -> int:
        return self._read(pos, 5, bigendian)

    def _read8(self, pos: int, bigendian: Optional[bool] = None) -> int:
        return self._read(pos, 8, bigendian)

    def _scale(self, value: int) -> int:
        if self._targetrate == self._sessionrate:
            return int(value)
        return int(_float32(_float32(value) * self._ratefactor))

    def foundat(self, haystack: bytes, needle: bytes) -> int:
        found = -1
        needle_n = len(needle)
        for i in range(len(haystack)):
            found = i
            for j in range(needle_n):
                if i + j >= len(haystack) or haystack[i + j] != needle[j]:
                    found = -1
                    break
            if found > 0:
                return found
        return -1

    def jumpto(self, currpos: int, maxoffset: int, needle: bytes) -> Optional[int]:
        k = currpos
        needlelen = len(needle)
        while k + needlelen < maxoffset:
            if self._ptfunxored[k : k + needlelen] == needle:
                return k
            k += 1
        return None

    @staticmethod
    def foundin(haystack: str, needle: str) -> bool:
        return needle in haystack

    def parsestring(self, pos: int) -> str:
        length = self._read4(pos)
        start = pos + 4
        return self._need(start, length).decode("latin-1")

    def parse_version(self) -> bool:
        failed = True
        if self._len < 0x41:
            return failed
        if self._ptfunxored[0] != 0x03 and self.foundat(self._ptfunxored[:0x100], BITCODE) != 1:
            return failed

        self.is_bigendian = bool(self._ptfunxored[0x11])
        block = self.parse_block_at(0x1F)
        if block is None:
            version = self._ptfunxored[0x40]
            if version == 0:
                version = self._ptfunxored[0x3D]
            if version == 0:
                version = self._ptfunxored[0x3A] + 2
            self._version = version
            return version == 0

        if block.content_type == 0x0003:
            skip = len(self.parsestring(block.offset + 3)) + 8
            self._version = self._read4(block.offset + 3 + skip)
            failed = False
        elif block.content_type == 0x2067:
            self._version = 2 + self._read4(block.offset + 20)
            failed = False
        return failed

    def setrates(self) -> None:
        self._ratefactor = 1.0
        if self._sessionrate:
            self._ratefactor = _float32(_float32(self._targetrate) / _float32(self._sessionrate))

    def parse_block_at(
        self,
        pos: int,
        parent: Optional[Block] = None,
        level: int = 0,
    ) -> Optional[Block]:
        del level
        if pos < 0 or pos + 9 > self._len:
            return None
        if self._ptfunxored[pos] != ZMARK:
            return None

        maxoffset = self._len if parent is None else parent.block_size + parent.offset
        try:
            block_type = self._read2(pos + 1)
            block_size = self._read4(pos + 3)
            content_type = self._read2(pos + 7)
        except PTFParseError:
            return None

        offset = pos + 7
        if block_size + offset > maxoffset:
            return None
        if block_type & 0xFF00:
            return None

        block = Block(
            zmark=ZMARK,
            block_type=block_type,
            block_size=block_size,
            content_type=content_type,
            offset=offset,
        )

        childjump = 0
        i = 1
        while i < block.block_size and pos + i + childjump < maxoffset:
            child = self.parse_block_at(pos + i, block)
            childjump = 0
            if child is not None:
                block.child.append(child)
                childjump = child.block_size + 7
            i += childjump if childjump else 1
        return block

    def parseblocks(self) -> None:
        i = 20
        while i < self._len:
            block = self.parse_block_at(i)
            if block is not None:
                self.blocks.append(block)
                i += block.block_size + 7 if block.block_size else 1
            else:
                i += 1

    def parse(self) -> int:
        self.parseblocks()
        if not self.parseheader():
            return -1
        self.setrates()
        if self._sessionrate < 44100 or self._sessionrate > 192000:
            return -2
        if not self.parseaudio():
            return -3
        if not self.parserest():
            return -4
        if not self.parsemidi():
            return -5
        self.parse_tempo_meter()
        return 0

    def parseheader(self) -> bool:
        found = False
        for block in self.blocks:
            if block.content_type == 0x1028:
                self._sessionrate = self._read4(block.offset + 4)
                found = True
        return found

    def parseaudio(self) -> bool:
        found = False
        nwavs = 0

        for block in self.blocks:
            if block.content_type != 0x1004:
                continue
            nwavs = self._read4(block.offset + 2)
            for child in block.child:
                if child.content_type != 0x103A:
                    continue
                pos = child.offset + 11
                n = 0
                while pos < child.offset + child.block_size and n < nwavs:
                    wavname = self.parsestring(pos)
                    pos += len(wavname) + 4
                    wavtype = self._need(pos, 4).decode("latin-1")
                    pos += 9

                    if ".grp" in wavname:
                        continue
                    if "Audio Files" in wavname:
                        continue
                    if "Fade Files" in wavname:
                        continue

                    if self._version < 10:
                        if not any(kind in wavtype for kind in ("WAVE", "EVAW", "AIFF", "FFIA")):
                            continue
                    else:
                        if wavtype[0] != "\0":
                            if not any(kind in wavtype for kind in ("WAVE", "EVAW", "AIFF", "FFIA")):
                                continue
                        elif ".wav" not in wavname and ".aif" not in wavname:
                            continue

                    found = True
                    self._audiofiles.append(Wav(filename=wavname, index=n))
                    n += 1

        if not found:
            return not (nwavs > 0)

        for block in self.blocks:
            if block.content_type != 0x1004:
                continue
            wav_index = 0
            for child in block.child:
                if child.content_type != 0x1003:
                    continue
                for grandchild in child.child:
                    if grandchild.content_type == 0x1001 and wav_index < len(self._audiofiles):
                        self._audiofiles[wav_index].length = self._read8(grandchild.offset + 8)
                        wav_index += 1
        return found

    def parse_three_point(self, j: int) -> tuple[int, int, int]:
        if self.is_bigendian:
            offsetbytes = (self._ptfunxored[j + 4] & 0xF0) >> 4
            lengthbytes = (self._ptfunxored[j + 3] & 0xF0) >> 4
            startbytes = (self._ptfunxored[j + 2] & 0xF0) >> 4
        else:
            offsetbytes = (self._ptfunxored[j + 1] & 0xF0) >> 4
            lengthbytes = (self._ptfunxored[j + 2] & 0xF0) >> 4
            startbytes = (self._ptfunxored[j + 3] & 0xF0) >> 4

        offset = self._read_variable_int(j + 5, offsetbytes)
        j += offsetbytes
        length = self._read_variable_int(j + 5, lengthbytes)
        j += lengthbytes
        start = self._read_variable_int(j + 5, startbytes)
        return start, offset, length

    def _read_variable_int(self, pos: int, nbytes: int) -> int:
        if nbytes == 5:
            return self._read5(pos, False)
        if nbytes == 4:
            return self._read4(pos, False)
        if nbytes == 3:
            return self._read3(pos, False)
        if nbytes == 2:
            return self._read2(pos, False)
        if nbytes == 1:
            return self._ptfunxored[pos]
        return 0

    def parse_region_info(self, j: int, block: Block, region: Region) -> None:
        start, sampleoffset, length = self.parse_three_point(j)
        findex = self._read4(block.offset + block.block_size)

        wav = Wav(index=findex, posabsolute=self._scale(start), length=self._scale(length))
        found = self.find_wav(findex)
        if found is not None:
            wav.filename = found.filename

        region.startpos = self._scale(start)
        region.sampleoffset = self._scale(sampleoffset)
        region.length = self._scale(length)
        region.wave = wav
        region.midi = []

    def parserest(self) -> bool:
        found = False
        rindex = 0

        for block in self.blocks:
            if block.content_type not in (0x100B, 0x262A):
                continue
            for child in block.child:
                if child.content_type not in (0x1008, 0x2629):
                    continue
                if not child.child:
                    continue
                found = True
                j = child.offset + 11
                regionname = self.parsestring(j)
                j += len(regionname) + 4

                region = Region(name=regionname, index=rindex)
                self.parse_region_info(j, child.child[0], region)
                self._regions.append(region)
                rindex += 1
            found = True

        for block in self.blocks:
            if block.content_type != 0x1015:
                continue
            for child in block.child:
                if child.content_type != 0x1014:
                    continue
                j = child.offset + 2
                trackname = self.parsestring(j)
                j += len(trackname) + 5
                nch = self._read4(j)
                j += 4
                for _ in range(min(nch, MAX_CHANNELS_PER_TRACK)):
                    ch_index = self._read2(j)
                    if self.find_track(ch_index) is None:
                        self._tracks.append(
                            Track(name=trackname, index=ch_index, reg=Region(index=65535))
                        )
                    j += 2

        for block in self.blocks:
            if block.content_type != 0x2519:
                continue
            tindex = 0
            mindex = 0
            for child in block.child:
                if child.content_type != 0x251A:
                    continue
                j = child.offset + 4
                trackname = self.parsestring(j)

                track = Track(name=trackname, index=mindex, reg=Region(index=65535))
                audio_track = self.find_track(tindex)
                if not (audio_track is not None and audio_track.name in trackname):
                    self._miditracks.append(track)
                    mindex += 1
                tindex += 1

        for block in self.blocks:
            if block.content_type == 0x1012:
                count = 0
                for child in block.child:
                    if child.content_type != 0x1011:
                        continue
                    for grandchild in child.child:
                        if grandchild.content_type != 0x100F:
                            continue
                        for entry in grandchild.child:
                            if entry.content_type != 0x100E:
                                continue
                            rawindex = self._read4(entry.offset + 4)
                            track = self.find_track(count)
                            region = self.find_region(rawindex)
                            if track is None or region is None:
                                continue
                            track.reg = region
                            if track.reg.index != 65535:
                                self._tracks.append(track)
                    found = True
                    count += 1
            elif block.content_type == 0x1054:
                count = 0
                for child in block.child:
                    if child.content_type != 0x1052:
                        continue
                    for grandchild in child.child:
                        if grandchild.content_type != 0x1050:
                            continue
                        if self._ptfunxored[grandchild.offset + 46] == 0x01:
                            continue
                        for entry in grandchild.child:
                            if entry.content_type != 0x104F:
                                continue
                            j = entry.offset + 4
                            rawindex = self._read4(j)
                            j += 5
                            start = self._read4(j)
                            track = self.find_track(count)
                            region = self.find_region(rawindex)
                            if track is None or region is None:
                                continue
                            track.reg = region
                            track.reg.startpos = self._scale(start)
                            if track.reg.index != 65535:
                                self._tracks.append(track)
                    found = True
                    count += 1

        self._tracks = [track for track in self._tracks if track.reg.index != 65535]
        if not self._tracks:
            return found

        self._tracks.sort(key=lambda track: track.index)
        self._renumber_tracks(self._tracks)
        return found

    @staticmethod
    def _renumber_tracks(tracks: List[Track]) -> None:
        idx = 1
        while idx < len(tracks):
            while idx < len(tracks) and tracks[idx].index == tracks[idx - 1].index:
                idx += 1
            if idx >= len(tracks):
                break
            diffn = tracks[idx].index - tracks[idx - 1].index - 1
            if diffn:
                for rest in tracks[idx:]:
                    rest.index -= diffn
            idx += 1

        first = tracks[0].index
        if first > 0:
            for track in tracks:
                track.index -= first

    def parse_tempo_meter(self) -> None:
        """Parse PT12 tempo and meter maps.

        Pro Tools stores these maps twice: once inside the lane container and
        once as a sibling top-level payload.  `self.blocks` only includes the
        top-level payload, so parsing those blocks avoids duplicate events.
        """

        for block in self.blocks:
            if block.content_type == 0x2028:
                self._tempoevents = self._parse_tempo_block(block)
                break

        for block in self.blocks:
            if block.content_type == 0x2029:
                self._meterevents = self._parse_meter_block(block)
                break

    def _parse_tempo_block(self, block: Block) -> List[TempoEvent]:
        events: List[TempoEvent] = []
        if block.block_size < 21:
            return events

        payload_len = self._read4(block.offset + 9)
        count = self._read4(block.offset + 13)
        if payload_len != 4 + count * 61:
            return events

        pos = block.offset + 21
        end = block.offset + block.block_size
        for _ in range(count):
            if pos + 61 > end:
                break
            tick = self._read5(pos + 30) - ZERO_TICKS
            bpm = struct.unpack("<d", self._need(pos + 40, 8))[0]
            ppq = self._read4(pos + 48)
            events.append(TempoEvent(pos=tick, bpm=bpm, ppq=ppq))
            pos += 61
        return events

    def _parse_meter_block(self, block: Block) -> List[MeterEvent]:
        events: List[MeterEvent] = []
        if block.block_size < 17:
            return events

        count = self._read4(block.offset + 13)
        pos = block.offset + 17
        end = block.offset + block.block_size
        for _ in range(count):
            if pos + 36 > end:
                break
            events.append(
                MeterEvent(
                    pos=self._read5(pos) - ZERO_TICKS,
                    ordinal=self._read4(pos + 8),
                    numerator=self._read4(pos + 12),
                    denominator=self._read4(pos + 16),
                )
            )
            pos += 36
        return events

    def parsemidi(self) -> bool:
        midichunks: List[MidiChunk] = []
        midiregion_chunks: List[int] = []
        regionnumber = 0
        midiregionname = ""

        for block in self.blocks:
            if block.content_type == 0x2000:
                k = block.offset
                block_end = block.block_size + block.offset
                while k + 35 < block_end:
                    max_pos = 0
                    midi: List[MidiEvent] = []
                    found_at = self.jumpto(k, block_end, b"MdNLB")
                    if found_at is None:
                        break
                    k = found_at + 11
                    n_midi_events = self._read4(k)
                    k += 4
                    zero_ticks = self._read5(k)
                    for _ in range(n_midi_events):
                        if k >= block_end:
                            break
                        if self._version >= 10:
                            midi_pos = self._read5(k) - ZERO_TICKS
                        else:
                            midi_pos = self._read5(k) - zero_ticks
                        midi_note = self._ptfunxored[k + 8]
                        midi_len = self._read5(k + 9)
                        midi_velocity = self._ptfunxored[k + 17]
                        if midi_pos + midi_len > max_pos:
                            max_pos = midi_pos + midi_len
                        midi.append(
                            MidiEvent(
                                pos=midi_pos,
                                length=midi_len,
                                note=midi_note,
                                velocity=midi_velocity,
                            )
                        )
                        k += 35
                    midichunks.append(MidiChunk(zero=zero_ticks, maxlen=max_pos, chunk=midi))

            elif block.content_type in (0x2002, 0x2634):
                for child in block.child:
                    if child.content_type not in (0x2001, 0x2633):
                        continue
                    for grandchild in child.child:
                        if grandchild.content_type not in (0x1007, 0x2628):
                            continue
                        j = grandchild.offset + 2
                        midiregionname = self.parsestring(j)
                        j += 4 + len(midiregionname)
                        self.parse_three_point(j)
                        rindex = self._read4(grandchild.offset + grandchild.block_size)
                        if rindex >= len(midichunks):
                            continue
                        midi_chunk = midichunks[rindex]
                        region = Region(
                            name=midiregionname,
                            index=regionnumber,
                            startpos=ZERO_TICKS,
                            sampleoffset=0,
                            length=midi_chunk.maxlen,
                            midi=[_copy_midi_event(event) for event in midi_chunk.chunk],
                        )
                        self._midiregions.append(region)
                        midiregion_chunks.append(rindex)
                        regionnumber += 1

        for block in self.blocks:
            if block.content_type != 0x262C:
                continue
            mindex = 0
            for child in block.child:
                if child.content_type != 0x262B:
                    continue
                for grandchild in child.child:
                    if grandchild.content_type != 0x2628:
                        continue
                    count = 0
                    j = grandchild.offset + 2
                    regionname = self.parsestring(j)
                    j += 4 + len(regionname)
                    self.parse_three_point(j)
                    n = self._read2(grandchild.offset + grandchild.block_size + 2)

                    for entry in grandchild.child:
                        if entry.content_type != 0x2523:
                            continue
                        count += 1

                    if not count and n < len(midichunks):
                        midi_chunk = midichunks[n]
                        region = Region(
                            name=midiregionname,
                            index=n,
                            startpos=ZERO_TICKS,
                            length=midi_chunk.maxlen,
                            midi=[_copy_midi_event(event) for event in midi_chunk.chunk],
                        )
                        self._midiregions.append(region)
                        midiregion_chunks.append(n)
                        mindex += 1
            del mindex

        for block in self.blocks:
            if block.content_type != 0x1058:
                continue
            count = 0
            for child in block.child:
                if child.content_type != 0x1057:
                    continue
                for grandchild in child.child:
                    if grandchild.content_type != 0x1056:
                        continue
                    for entry in grandchild.child:
                        if entry.content_type != 0x104F:
                            continue
                        j = entry.offset + 4
                        rawindex = self._read4(j)
                        j += 5
                        start = self._read5(j)
                        track = self.find_miditrack(count)
                        region = self.find_midiregion(rawindex)
                        if track is None or region is None:
                            continue
                        track.reg = region
                        track.reg.startpos = self._scale(abs(int(start) - ZERO_TICKS))
                        chunk_index = -1
                        if rawindex < len(midiregion_chunks):
                            chunk_index = midiregion_chunks[rawindex]
                        self._midiplacements.append(
                            MidiPlacement(
                                track_index=count,
                                track_name=track.name,
                                region_index=rawindex,
                                region_name=region.name,
                                chunk_index=chunk_index,
                                startpos=track.reg.startpos,
                                midi=[_copy_midi_event(event) for event in region.midi],
                            )
                        )
                        if track.reg.index != 65535:
                            self._miditracks.append(track)
                count += 1

        self._miditracks = [track for track in self._miditracks if track.reg.index != 65535]
        return True

    def dump_blocks(self) -> str:
        lines: List[str] = []
        for block in self.blocks:
            self._dump_block(block, 0, lines)
        return "".join(lines)

    def _dump_block(self, block: Block, level: int, lines: List[str]) -> None:
        indent = "    " * level
        lines.append(f"{indent}{self.get_content_description(block.content_type)}(0x{block.content_type:04x})\n")
        lines.extend(self._hexdump(block.offset, block.block_size, level))
        for child in block.child:
            self._dump_block(child, level + 1, lines)

    def _hexdump(self, offset: int, length: int, level: int) -> List[str]:
        lines = []
        indent = "    " * level
        data = self._ptfunxored[offset : offset + length]
        for pos in range(0, len(data), 16):
            chunk = data[pos : pos + 16]
            hex_part = " ".join(f"{byte:02X}" for byte in chunk)
            ascii_part = "".join(chr(byte) if 32 < byte < 128 else "." for byte in chunk)
            lines.append(f"{indent}{hex_part} {ascii_part}\n")
        return lines
