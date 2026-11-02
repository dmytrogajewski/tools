"""A small 32-bit x86 instruction decoder: lengths and operand positions.

Enough of the one- and two-byte opcode maps to walk the code VC6 and VC7.1
emit (integer, x87, MMX/SSE up to SSE2), so that a checker can tell which
instruction owns a byte -- a relocation's field, say -- rather than guessing
from the bytes in front of it. fpconst.py once read `d8` as an x87 opcode
when it was the tail of the `fstp st(0)` (dd d8) before a `mov esi, imm32`.

decode(code, off) -> Insn or None (None: an opcode this does not know, or a
16-bit address-size prefix; the caller stops walking there).
"""

from collections import namedtuple

# off: the instruction's start; length; op: the opcode (0x0fXX for the two-
# byte map); reg: the modrm reg field (None without a modrm); mod/rm the
# same; disp_off/disp_len: where its displacement sits (absolute offset into
# code; None if it has none); imm_off/imm_len: the same for its immediate;
# prefixes: the prefix bytes, in order.
Insn = namedtuple("Insn", "off length op reg mod rm disp_off disp_len "
                          "imm_off imm_len prefixes")

PREFIXES = {0x26, 0x2E, 0x36, 0x3E, 0x64, 0x65, 0x66, 0x67, 0xF0, 0xF2, 0xF3}

# one-byte opcodes with a modrm byte
_M1 = set()
for b in range(0x00, 0x40, 8):
    _M1.update(range(b, b + 4))
_M1.update({0x62, 0x63, 0x69, 0x6B, 0xC0, 0xC1, 0xC4, 0xC5, 0xC6, 0xC7,
            0xD0, 0xD1, 0xD2, 0xD3, 0xF6, 0xF7, 0xFE, 0xFF})
_M1.update(range(0x80, 0x90))
_M1.update(range(0xD8, 0xE0))

# one-byte opcodes' immediates: 1 byte, 2 bytes, or "z" (4, 2 under 0x66)
_I1 = {}
for b in (0x04, 0x0C, 0x14, 0x1C, 0x24, 0x2C, 0x34, 0x3C, 0x6A, 0x6B, 0x80,
          0x82, 0x83, 0xA8, 0xC0, 0xC1, 0xC6, 0xCD, 0xD4, 0xD5, 0xEB):
    _I1[b] = 1
for b in list(range(0x70, 0x80)) + list(range(0xB0, 0xB8)) + list(range(0xE0, 0xE8)):
    _I1[b] = 1
for b in (0x05, 0x0D, 0x15, 0x1D, 0x25, 0x2D, 0x35, 0x3D, 0x68, 0x69, 0x81,
          0xA9, 0xC7, 0xE8, 0xE9):
    _I1[b] = "z"
for b in range(0xB8, 0xC0):
    _I1[b] = "z"
_I1[0xC2] = _I1[0xCA] = 2
_I1[0xC8] = 3

# one-byte opcodes with no operand bytes at all
_N1 = set(range(0x40, 0x62)) | set(range(0x90, 0x9A)) | {
    0x06, 0x07, 0x0E, 0x16, 0x17, 0x1E, 0x1F, 0x27, 0x2F, 0x37, 0x3F,
    0x6C, 0x6D, 0x6E, 0x6F, 0x9B, 0x9C, 0x9D, 0x9E, 0x9F, 0xA4, 0xA5, 0xA6,
    0xA7, 0xAA, 0xAB, 0xAC, 0xAD, 0xAE, 0xAF, 0xC3, 0xC9, 0xCB, 0xCC, 0xCE,
    0xCF, 0xD6, 0xD7, 0xEC, 0xED, 0xEE, 0xEF, 0xF1, 0xF4, 0xF5, 0xF8, 0xF9,
    0xFA, 0xFB, 0xFC, 0xFD}

# two-byte (0x0f) map: modrm, and modrm plus an imm8
_M2 = (set(range(0x10, 0x18)) | set(range(0x18, 0x20)) | set(range(0x28, 0x30))
       | set(range(0x40, 0x50)) | set(range(0x50, 0x70)) | set(range(0x74, 0x77))
       | set(range(0x7E, 0x80)) | set(range(0x90, 0xA0)) | set(range(0xD0, 0x100))
       | {0x00, 0x01, 0x02, 0x03, 0x0D, 0x20, 0x21, 0x22, 0x23, 0xA3, 0xA5,
          0xAB, 0xAD, 0xAE, 0xAF, 0xB0, 0xB1, 0xB2, 0xB3, 0xB4, 0xB5, 0xB6,
          0xB7, 0xBB, 0xBC, 0xBD, 0xBE, 0xBF, 0xC0, 0xC1, 0xC3, 0xC7, 0x7C, 0x7D})
_M2I = {0x70, 0x71, 0x72, 0x73, 0xA4, 0xAC, 0xBA, 0xC2, 0xC4, 0xC5, 0xC6}
_N2 = {0x05, 0x06, 0x07, 0x08, 0x09, 0x0B, 0x30, 0x31, 0x32, 0x33, 0x34,
       0x35, 0x77, 0xA0, 0xA1, 0xA2, 0xA8, 0xA9, 0xAA} | set(range(0xC8, 0xD0))


def _modrm(code, i):
    """(length of modrm+sib+disp, mod, reg, rm, disp offset, disp length)."""
    m = code[i]
    mod, reg, rm = m >> 6, (m >> 3) & 7, m & 7
    n = 1
    if mod == 3:
        return n, mod, reg, rm, None, 0
    if rm == 4:
        if i + 1 >= len(code):
            return None
        base = code[i + 1] & 7
        n += 1
        if mod == 0 and base == 5:
            return n + 4, mod, reg, rm, i + n, 4
    elif mod == 0 and rm == 5:
        return n + 4, mod, reg, rm, i + n, 4
    if mod == 1:
        return n + 1, mod, reg, rm, i + n, 1
    if mod == 2:
        return n + 4, mod, reg, rm, i + n, 4
    return n, mod, reg, rm, None, 0


def decode(code, off):
    i = off
    pre = []
    while i < len(code) and code[i] in PREFIXES:
        pre.append(code[i])
        i += 1
    if 0x67 in pre or i >= len(code):
        return None
    o16 = 0x66 in pre
    z = 2 if o16 else 4
    op = code[i]
    i += 1
    reg = mod = rm = None
    disp_off = None
    disp_len = 0
    imm = 0
    if op == 0x0F:
        if i >= len(code):
            return None
        op2 = code[i]
        i += 1
        opc = 0x0F00 | op2
        if 0x80 <= op2 <= 0x8F:
            imm = z
        elif op2 in _N2:
            pass
        elif op2 in _M2 or op2 in _M2I:
            if i >= len(code):
                return None
            r = _modrm(code, i)
            if r is None:
                return None
            n, mod, reg, rm, disp_off, disp_len = r
            i += n
            if op2 in _M2I:
                imm = 1
        else:
            return None
    else:
        opc = op
        if op in _M1:
            if i >= len(code):
                return None
            r = _modrm(code, i)
            if r is None:
                return None
            n, mod, reg, rm, disp_off, disp_len = r
            i += n
            if op == 0xF6 and reg in (0, 1):
                imm = 1
            elif op == 0xF7 and reg in (0, 1):
                imm = z
        elif op in _N1:
            pass
        elif op in (0xA0, 0xA1, 0xA2, 0xA3):
            disp_off, disp_len = i, 4           # moffs32
            i += 4
        elif op in (0x9A, 0xEA):
            imm = z + 2
        elif op not in _I1:
            return None
        if op in _I1:
            k = _I1[op]
            imm = z if k == "z" else k
    imm_off = i if imm else None
    i += imm
    if i > len(code):
        return None
    return Insn(off, i - off, opc, reg, mod, rm, disp_off, disp_len,
                imm_off, imm, tuple(pre))


def walk(code, start=0, end=None):
    """Yield code's instructions from start, linearly, until end or the first
    opcode decode() does not know."""
    end = len(code) if end is None else end
    i = start
    while i < end:
        ins = decode(code, i)
        if ins is None:
            return
        yield ins
        i += ins.length
