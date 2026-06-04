"""Low-level Pro Tools `.ptx` I/O primitives.

The minimal foundation the rest of the library builds on:

- `load_unxored` / `encrypt_session_data` — de/obfuscate a `.ptx` (the file is XOR-masked).
- `parse_unxored` — parse de-obfuscated bytes into the `core.PTFFormat` block tree.
- `top_level_refs` / `BlockRef` — the ordered top-level blocks (zmark, type, offset, size).
- `_flatten_block_bounds` — (zmark, end, content_type) for every block, used by `final_index`.

Master-index (0x0002) repair lives in `final_index` (the structured "holes" model). The
older heuristic offset-guesser that used to share this module — `_update_final_index` &
friends — is deprecated and archived in `unused/writer_legacy.py`; nothing here uses it.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .core import Block, PTFFormat


@dataclass(frozen=True)
class BlockRef:
    block: Block
    start: int
    end: int
    data: bytes


def encrypt_session_data(unxored: bytes) -> bytes:
    """Apply the Pro Tools session XOR pass to unencrypted session bytes."""

    out = bytearray(unxored)
    if len(out) < 0x14:
        raise ValueError("session is too short")

    xor_type = out[0x12]
    xor_value = out[0x13]
    if xor_type == 0x01:
        xor_delta = PTFFormat.gen_xor_delta(xor_value, 53, False)
    elif xor_type == 0x05:
        xor_delta = PTFFormat.gen_xor_delta(xor_value, 11, True)
    else:
        raise ValueError(f"unsupported XOR type {xor_type:#x}")

    xor_key = [(i * xor_delta) & 0xFF for i in range(256)]
    for i in range(0x14, len(out)):
        xor_index = (i & 0xFF) if xor_type == 0x01 else ((i >> 12) & 0xFF)
        out[i] ^= xor_key[xor_index]
    return bytes(out)


def load_unxored(path: str | Path) -> bytes:
    ptf = PTFFormat()
    if ptf.unxor(path) != 0:
        raise ValueError(f"cannot decrypt {path}")
    return ptf.unxored_data()


def parse_unxored(data: bytes) -> PTFFormat:
    ptf = PTFFormat()
    ptf._ptfunxored = data
    ptf._len = len(data)
    if ptf.parse_version():
        raise ValueError("cannot extract Pro Tools version")
    ptf.parseblocks()
    return ptf


def top_level_refs(data: bytes) -> list[BlockRef]:
    ptf = parse_unxored(data)
    return [
        BlockRef(
            block=block,
            start=block.offset - 7,
            end=block.offset + block.block_size,
            data=data[block.offset - 7 : block.offset + block.block_size],
        )
        for block in ptf.blocks
    ]


def _flatten_block_bounds(data: bytes) -> list[tuple[int, int, int]]:
    ptf = parse_unxored(data)
    bounds: list[tuple[int, int, int]] = []

    def visit(block: Block) -> None:
        bounds.append((block.offset - 7, block.offset + block.block_size, block.content_type))
        for child in sorted(block.child, key=lambda item: item.offset):
            visit(child)

    for block in sorted(ptf.blocks, key=lambda item: item.offset):
        visit(block)
    return bounds
