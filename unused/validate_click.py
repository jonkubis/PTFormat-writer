"""Click-synthesis validation harness (see docs/CLICK-HANDOFF.md).

Reproduces `1 stereo plus click.ptx` from `1 stereo tracks.ptx` via the structural
diff-replay in ptxformatwriter.click_clone, and reports byte-exactness + the first
divergence. Goal: byte_exact=True (a lone diff inside the 0x2067 session-name block
is acceptable). Run: python3 validate_click.py
"""
import sys
sys.path.insert(0, ".")
from pathlib import Path
from collections import Counter
from ptxformatwriter.core import PTFFormat
from ptxformatwriter import body_synth as BS, final_index as FI, click_clone as CC
import ptxformatwriter.writer as W

STER = Path("control_files/lots of stereo tracks")


def load(p):
    ptf = PTFFormat(); ptf.load(str(p), 48000); return ptf.unxored_data()


def main():
    s1 = load(STER / "1 stereo tracks.ptx")
    ctrl = load(STER / "1 stereo plus click.ptx")

    patch = CC.derive_click_patch(s1, ctrl)
    print("patch: replacements=%d total_new_bytes=%d"
          % (len(patch.replacements), sum(len(r[2]) for r in patch.replacements)))

    out = CC.apply_click_patch(s1, patch, s1)
    open("control_files/synth_stereo_click.ptx", "wb").write(W.encrypt_session_data(out))
    rc = PTFFormat().load("control_files/synth_stereo_click.ptx", 48000)

    def cnt(d):
        return Counter(b.content_type for b in BS.flat_blocks(BS.parse(d)))
    co, cc = cnt(out), cnt(ctrl)
    diffs = {hex(t): (co.get(t, 0), cc.get(t, 0))
             for t in set(co) | set(cc) if co.get(t, 0) != cc.get(t, 0)}

    # Bytes 0x12/0x13 are the (un-xored) XOR key type+value; Pro Tools re-rolls them
    # on every save, so they legitimately differ donor vs control and are not part of
    # the session body. Neutralize them before the byte-exact check (same spirit as the
    # allowed 0x2067 session-name diff).
    def _strip_seed(b):
        b = bytearray(b)
        if len(b) >= 0x14:
            b[0x12:0x14] = b"\x00\x00"
        return bytes(b)
    out_cmp, ctrl_cmp = _strip_seed(out), _strip_seed(ctrl)

    print("reload_rc=%d out_sz=%d ctrl_sz=%d byte_exact=%s" % (rc, len(out), len(ctrl), out_cmp == ctrl_cmp))
    print("per-type block-count diffs (out vs ctrl):", diffs)

    # first byte divergence + which block it falls in
    n = min(len(out_cmp), len(ctrl_cmp))
    for i in range(n):
        if out_cmp[i] != ctrl_cmp[i]:
            blk = None
            for b in BS.flat_blocks(BS.parse(ctrl)):
                if b.offset - 7 <= i < b.offset + b.block_size:
                    blk = b
            bt = ("0x%04x" % blk.content_type) if blk else "?"
            print("first byte diff @%d (in block %s): out=%s ctrl=%s"
                  % (i, bt, out[max(0, i - 4):i + 8].hex(), ctrl[max(0, i - 4):i + 8].hex()))
            break
    else:
        if len(out) != len(ctrl):
            print("identical up to %d; lengths differ (out %d vs ctrl %d)" % (n, len(out), len(ctrl)))


if __name__ == "__main__":
    main()
