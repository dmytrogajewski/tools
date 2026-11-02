"""Adversarial fixtures for the scoring tools: small synthetic objects and
images, each built to catch one way the oracle has been, or could be,
fooled.

    python3 -m unittest discover -s tools/tests     (from a project), or
    python3 -m unittest discover -s tests           (from tools/ itself)

Covered: a wrong float literal behind a masked relocation (fpconst.py); a
relocated `mov reg, imm32` after an `fstp st(0)` not taken for an x87
operand (fpconst.py, x86len.py); instruction lengths (x86len.py); an
interior match, a shared tail and a non-start hit (progress.py's
_resolve_overlaps); whole-image identity through the time stamps but not
through a changed byte (progress.py, buildcmp.py); known and new data
differences (datacheck.py). Not covered here, by their nature: vtabcheck.py
and the region rules need a linked image and a map (tested against the
games' builds: see tools/README.md).
"""
import os
import struct
import sys
import tempfile
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.dirname(HERE)
sys.path.insert(0, TOOLS)

# progress.py reads a project's progress/targets.py at import: a stub
stub = types.ModuleType("targets")
stub.REFERENCE, stub.OUT, stub.DATA = "out/reference", "out/progress", "progress"
stub.TARGETS, stub.LIBS, stub.DRIVER_OBJS, stub.SHIPPED_ENV = [], [], set(), None
sys.modules.setdefault("targets", stub)

import fpconst  # noqa: E402
import progress  # noqa: E402
import x86len  # noqa: E402
import datacheck  # noqa: E402

DIR32 = 6


def coff(code, relocs, rdata):
    """A COFF object: .text holding one function `_f` (code), .rdata holding
    `_lit` (rdata), and DIR32 relocations at the given .text offsets, all to
    `_lit`."""
    nsec = 2
    hdr = 20
    sh = hdr + 40 * nsec
    text_p = sh
    rel_p = text_p + len(code)
    rdata_p = rel_p + 10 * len(relocs)
    sym_p = rdata_p + len(rdata)
    syms = [(b"_f", 0, 1, 0x20, 2), (b"_lit", 0, 2, 0, 2)]
    out = bytearray(struct.pack("<HHIIIHH", 0x14C, nsec, 0, sym_p, len(syms), 0, 0))
    out += struct.pack("<8sIIIIIIHHI", b".text", 0, 0, len(code), text_p, rel_p, 0,
                       len(relocs), 0, 0x60000020)
    out += struct.pack("<8sIIIIIIHHI", b".rdata", 0, 0, len(rdata), rdata_p, 0, 0,
                       0, 0, 0x40000040)
    out += code
    for off in relocs:
        out += struct.pack("<IIH", off, 1, DIR32)
    out += rdata
    for name, val, sec, typ, st in syms:
        out += struct.pack("<8sIhHBB", name, val, sec, typ, st, 0)
    out += struct.pack("<I", 4)          # an empty string table
    return bytes(out)


def pe(text, rdata, base=0x400000, stamp=0):
    """A minimal PE image: .text at base+0x1000, .rdata at base+0x2000."""
    out = bytearray(0x200)
    out[0:2] = b"MZ"
    struct.pack_into("<I", out, 0x3C, 0x40)
    out[0x40:0x44] = b"PE\0\0"
    struct.pack_into("<HHIIIHH", out, 0x44, 0x14C, 2, stamp, 0, 0, 0xE0, 0x102)
    o = 0x58
    struct.pack_into("<H", out, o, 0x10B)
    struct.pack_into("<I", out, o + 16, 0x1000)
    struct.pack_into("<I", out, o + 28, base)
    struct.pack_into("<II", out, o + 32, 0x1000, 0x200)
    struct.pack_into("<I", out, o + 56, 0x3000)
    struct.pack_into("<I", out, o + 60, 0x200)
    struct.pack_into("<I", out, o + 92, 16)
    s = o + 0xE0
    tsz = (len(text) + 0x1FF) & ~0x1FF
    rsz = (len(rdata) + 0x1FF) & ~0x1FF
    struct.pack_into("<8sIIIIIIHHI", out, s, b".text", len(text), 0x1000, tsz,
                     0x200, 0, 0, 0, 0, 0x60000020)
    struct.pack_into("<8sIIIIIIHHI", out, s + 40, b".rdata", len(rdata), 0x2000,
                     rsz, 0x200 + tsz, 0, 0, 0, 0, 0x40000040)
    out += text.ljust(tsz, b"\xcc") + rdata.ljust(rsz, b"\0")
    return bytes(out)


class Tmp:
    def __init__(self):
        self.d = tempfile.mkdtemp()

    def write(self, name, data):
        p = os.path.join(self.d, name)
        with open(p, "wb") as f:
            f.write(data)
        return p


FILLER = bytes.fromhex("d9c0d9c0d9c0d9c0ddd8ddd8ddd8ddd8") + b"\xc3"


class FloatConstants(unittest.TestCase):
    def run_case(self, obj_lit, img_lit, prefix=b"\xd9\x05"):
        # fld dword [lit] ; filler ; ret
        code = prefix + b"\0\0\0\0" + FILLER
        t = Tmp()
        o = t.write("f.obj", coff(code, [len(prefix)], struct.pack("<f", obj_lit)))
        img_code = prefix + struct.pack("<I", 0x402000) + FILLER
        p = t.write("img.exe", pe(img_code, struct.pack("<f", img_lit)))
        import io
        return fpconst.check(p, [o], out=io.StringIO())

    def test_wrong_literal_is_caught(self):
        checked, bad, _u = self.run_case(1.0, 0.001)
        self.assertEqual((checked, bad), (1, 1))

    def test_right_literal_passes(self):
        checked, bad, _u = self.run_case(0.001, 0.001)
        self.assertEqual((checked, bad), (1, 0))

    def test_mov_after_fstp_is_not_an_x87_operand(self):
        # `fstp st(0)` (dd d8) then `mov esi, imm32`: the byte before the
        # field is d8, which the old checker took for an x87 opcode
        checked, bad, _u = self.run_case(1.0, 0.001, prefix=b"\xdd\xd8\xbe")
        self.assertEqual((checked, bad), (0, 0))


class Lengths(unittest.TestCase):
    def test_lengths(self):
        cases = [
            ("d905" + "00" * 4, 6, 2),        # fld dword [abs32]
            ("ddd8", 2, None),                # fstp st(0)
            ("be" + "00" * 4, 5, None),       # mov esi, imm32
            ("8b442404", 4, 3),               # mov eax, [esp+4] (disp8)
            ("c7058c000000" + "00" * 4, 10, 2),  # mov dword [abs], imm32
            ("0fb6c0", 3, None),              # movzx eax, al
            ("66c7050000000001 00".replace(" ", ""), 9, 3),  # mov word [abs], imm16
            ("e8" + "00" * 4, 5, None),       # call rel32
            ("0f84" + "00" * 4, 6, None),     # je rel32
        ]
        for hexs, n, disp in cases:
            ins = x86len.decode(bytes.fromhex(hexs), 0)
            self.assertIsNotNone(ins, hexs)
            self.assertEqual(ins.length, n, hexs)
            self.assertEqual(ins.disp_off, disp, hexs)


class Overlaps(unittest.TestCase):
    def resolve(self, matched, starts, names=None, placed=()):
        progress.PLACED_SYMS.clear()
        for a, n in (names or {}).items():
            progress.PLACED_SYMS[("T", a)] = n
        progress._resolve_overlaps("T", matched, set(placed), set(starts), {},
                                   {}, {}, lambda *a: iter(()), b"", 0)
        return matched

    def test_interior_match_is_dropped(self):
        m = self.resolve({0x1000: 91, 0x1014: 71, 0x101e: 61}, starts={0x1000})
        self.assertEqual(m, {0x1000: 91})

    def test_shared_tail_is_clipped_not_dropped(self):
        m = self.resolve({0x1000: 21, 0x100c: 247}, starts={0x1000, 0x100c})
        self.assertEqual(m, {0x1000: 12, 0x100c: 247})

    def test_start_beats_a_longer_non_start(self):
        m = self.resolve({0x0ff8: 40, 0x1000: 16}, starts={0x1000})
        self.assertEqual(m, {0x1000: 16})

    def test_no_byte_counted_twice(self):
        m = self.resolve({0x1000: 30, 0x1008: 8, 0x1010: 40, 0x1030: 4},
                         starts={0x1000, 0x1010, 0x1030})
        spans = sorted((a, a + n) for a, n in m.items())
        for (a0, e0), (a1, _e1) in zip(spans, spans[1:]):
            self.assertLessEqual(e0, a1)


class WholeImage(unittest.TestCase):
    def test_time_stamps_only(self):
        t = Tmp()
        text = b"\x55\x8b\xec\x5d\xc3"
        a = t.write("a.exe", pe(text, b"x" * 8, stamp=1))
        b = t.write("b.exe", pe(text, b"x" * 8, stamp=2))
        c = t.write("c.exe", pe(text, b"y" * 8, stamp=1))
        old = progress.REPO
        progress.REPO = "/"
        try:
            self.assertTrue(progress.whole_image_identical({"shipped": a}, b))
            self.assertFalse(progress.whole_image_identical({"shipped": a}, c))
        finally:
            progress.REPO = old


class DataCheck(unittest.TestCase):
    def test_known_and_new(self):
        import collections
        diff = {("x.dll", "retail"): collections.Counter({"a": 1, "b": 2}),
                ("x.dll", "ours"): collections.Counter()}
        known = [("x.dll", "retail", "a", "#"), ("x.dll", "retail", "b", "#"),
                 ("x.dll", "retail", "gone", "#")]
        new, fixed = datacheck.compare(diff, known)
        self.assertEqual(dict(new), {("x.dll", "retail", "b"): 1})
        self.assertEqual(dict(fixed), {("x.dll", "retail", "gone"): 1})

    def test_wildcard(self):
        import collections
        diff = {("x.dll", "retail"): collections.Counter({"a": 3})}
        new, _f = datacheck.compare(diff, [("x.dll", "retail", "*", "#")])
        self.assertFalse(new)


if __name__ == "__main__":
    unittest.main()
