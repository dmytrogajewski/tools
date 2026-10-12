#!/usr/bin/env python3
"""Read a classic Mac OS PEF executable: the Mac release's LithTech engine.

The Mac NOLF (rez/games/nolf-mac-usa/NOLF/LithTech, Hyperion's 2002 port)
is a PowerPC PEF ("Preferred Executable Format") application built with
CodeWarrior. CodeWarrior keeps each function's name, C++-mangled, in the
traceback table it writes after the function, so the engine is named
throughout -- including its OpenGL renderer, which the Mac links into the
engine where the PC has d3d.ren. This reads all of that:

  tools/pef.py info  <pef>                 sections, imported libraries
  tools/pef.py funcs <pef> [regex]         every named function:
                                           start end size name
  tools/pef.py dis   <pef> <name|0xaddr>   disassemble one function, with
                                           TOC loads, calls and imports named
  tools/pef.py refs  <pef> <name>          functions that take the address of
                                           <name> (a TOC load of its
                                           transition vector) or call it
  tools/pef.py imports <pef> [regex]       imported symbols and their library

<pef> defaults to the Mac engine when given as "-". A name argument matches
a function whose mangled name starts with it (r_Start3D matches
r_Start3D__Fv). Addresses are offsets into the code section.

How it works, for the next reader:
  * The code section is raw; the data section is "pattern-initialized" and
    unpacked here (the five PEF pattern opcodes).
  * Functions: each traceback table ends with a two-byte name length and the
    name; the table's tb_offset field (just before the name) is the distance
    back from the table to the function's first instruction.
  * Code reaches data through r2, the TOC. The main symbol's transition
    vector (loader header) gives the TOC's data offset. A TOC slot holds the
    data offset of a transition vector, whose first word is the code offset
    of the function -- both unrelocated, so they read as plain offsets.
  * Imports: the loader's relocation program is run to find which data words
    the loader fills with an imported symbol, and calls into another
    library go through a glue stub (`lwz r12,toc(r2) ... bctr`) that names
    the slot.

Disassembly needs capstone (https://www.capstone-engine.org). It is not in
the build image; put its Python package on PYTHONPATH (for example unpack
the capstone wheel from PyPI into a directory and point PYTHONPATH at it).
Everything except `dis` and `refs` works without it.
"""
import bisect
import os
import re
import struct
import sys

DEFAULT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "rez",
                       "games", "nolf-mac-usa", "NOLF", "LithTech")


def vnum(b, i):
    v = 0
    while True:
        c = b[i]
        i += 1
        v = (v << 7) | (c & 0x7f)
        if not c & 0x80:
            return v, i


def unpack_pattern(p, size):
    out = bytearray()
    i = 0
    while i < len(p):
        op, cnt = p[i] >> 5, p[i] & 0x1f
        i += 1
        if cnt == 0:
            cnt, i = vnum(p, i)
        if op == 0:                          # zero
            out += bytes(cnt)
        elif op == 1:                        # block copy
            out += p[i:i + cnt]
            i += cnt
        elif op == 2:                        # repeated block
            rep, i = vnum(p, i)
            out += p[i:i + cnt] * (rep + 1)
            i += cnt
        elif op in (3, 4):                   # interleaved repeat (block / zero)
            cust, i = vnum(p, i)
            rep, i = vnum(p, i)
            if op == 3:
                common = p[i:i + cnt]
                i += cnt
            else:
                common = bytes(cnt)
            out += common
            for _ in range(rep):
                out += p[i:i + cust]
                i += cust
                out += common
        else:
            raise ValueError("bad pattern opcode %d" % op)
    if len(out) != size:
        raise ValueError("pattern data unpacked to %d bytes, wanted %d" % (len(out), size))
    return bytes(out)


class PEF:
    def __init__(self, path):
        b = open(path, "rb").read()
        if b[:12] != b"Joy!peffpwpc":
            raise ValueError("%s: not a PowerPC PEF container" % path)
        nsec, = struct.unpack_from(">H", b, 0x20)
        self.sections = []
        for i in range(nsec):
            (nameoff, addr, total, unpacked, csize, coff, kind, share, align,
             _) = struct.unpack_from(">IIIIIIBBBB", b, 40 + 28 * i)
            self.sections.append(dict(index=i, kind=kind, size=csize, unpacked=unpacked,
                                      total=total, offset=coff))
        code = next(s for s in self.sections if s["kind"] == 0)
        data = next(s for s in self.sections if s["kind"] in (1, 2))
        loader = next(s for s in self.sections if s["kind"] == 4)
        self.code_index, self.data_index = code["index"], data["index"]
        self.code = b[code["offset"]:code["offset"] + code["size"]]
        raw = b[data["offset"]:data["offset"] + data["size"]]
        self.data = unpack_pattern(raw, data["unpacked"]) if data["kind"] == 2 else raw
        self.loader = b[loader["offset"]:loader["offset"] + loader["size"]]
        self._read_loader()
        self._find_functions()

    # -- loader: main symbol, imports, relocations -------------------------
    def _read_loader(self):
        L = self.loader
        (msec, moff, isec, ioff, tsec, toff, nlibs, nsyms, nrelsec, relinstr,
         strs, hashoff, hashpow, nexp) = struct.unpack_from(">iIiIiIIIIIIIII", L, 0)
        self.toc = struct.unpack_from(">I", self.data, moff + 4)[0] if msec == self.data_index else None
        name = lambda o: L[strs + o:L.index(b"\0", strs + o)].decode("latin-1")
        self.libs, self.imports = [], []
        p = 56
        for _ in range(nlibs):
            no, _a, _b, cnt, first, _o, _r1, _r2 = struct.unpack_from(">IIIIIBBH", L, p)
            self.libs.append((name(no), first, cnt))
            p += 24
        for k in range(nsyms):
            w, = struct.unpack_from(">I", L, p + 4 * k)
            self.imports.append(name(w & 0xffffff))
        self.import_lib = {}
        for lib, first, cnt in self.libs:
            for k in range(first, first + cnt):
                self.import_lib[k] = lib
        p += 4 * nsyms
        self.import_at = {}                  # data offset -> import index
        for _ in range(nrelsec):
            sec, _r, cnt, first = struct.unpack_from(">HHII", L, p)
            p += 12
            if sec == self.data_index:
                self._run_relocs(L[relinstr + first:relinstr + first + 2 * cnt])

    def _run_relocs(self, prog):
        words = struct.unpack(">%dH" % (len(prog) // 2), prog)
        pos, imp = 0, 0
        i = 0
        history = []
        while i < len(words):
            w = words[i]
            start = i
            i += 1
            if w >> 14 == 0:                                  # BySectDWithSkip
                pos += 4 * ((w >> 6) & 0xff) + 4 * (w & 0x3f)
            elif w >> 13 == 0b010:                            # value group
                sub, run = (w >> 9) & 0xf, (w & 0x1ff) + 1
                if sub in (0, 1):
                    pos += 4 * run
                elif sub == 2:
                    pos += 12 * run
                elif sub in (3, 4):
                    pos += 8 * run
                elif sub == 5:                                # ImportRun
                    for _ in range(run):
                        self.import_at[pos] = imp
                        imp += 1
                        pos += 4
            elif w >> 13 == 0b011:                            # small by index
                sub, idx = (w >> 9) & 0xf, w & 0x1ff
                if sub == 0:                                  # SmByImport
                    self.import_at[pos] = idx
                    imp = idx + 1
                    pos += 4
                elif sub == 3:                                # SmBySection
                    pos += 4
            elif w >> 12 == 0b1000:                           # IncrPosition
                pos += (w & 0xfff) + 1
            elif w >> 12 == 0b1001:                           # SmRepeat
                blocks, reps = ((w >> 8) & 0xf) + 1, (w & 0xff) + 1
                body = history[-blocks:]
                for _ in range(reps):
                    for bw in body:
                        pos, imp = self._step(bw, pos, imp)
            elif w >> 10 == 0b101000:                         # SetPosition
                pos = ((w & 0x3ff) << 16) | words[i]
                i += 1
            elif w >> 10 == 0b101001:                         # LgByImport
                idx = ((w & 0x3ff) << 16) | words[i]
                i += 1
                self.import_at[pos] = idx
                imp = idx + 1
                pos += 4
            elif w >> 10 == 0b101100:                         # LgRepeat
                blocks = ((w >> 6) & 0xf) + 1
                reps = (((w & 0x3f) << 16) | words[i])
                i += 1
                body = history[-blocks:]
                for _ in range(reps):
                    for bw in body:
                        pos, imp = self._step(bw, pos, imp)
            elif w >> 10 == 0b101101:                         # LgSetOrBySection
                i += 1
                if (w >> 6) & 0xf == 0:
                    pos += 4
            else:
                raise ValueError("unknown relocation opcode %#06x" % w)
            history.append(words[start:i])

    def _step(self, ws, pos, imp):
        """Replay one already-decoded instruction (for the repeat opcodes)."""
        w = ws[0]
        if w >> 14 == 0:
            return pos + 4 * ((w >> 6) & 0xff) + 4 * (w & 0x3f), imp
        if w >> 13 == 0b010:
            sub, run = (w >> 9) & 0xf, (w & 0x1ff) + 1
            if sub == 5:
                for _ in range(run):
                    self.import_at[pos] = imp
                    imp += 1
                    pos += 4
                return pos, imp
            return pos + {0: 4, 1: 4, 2: 12, 3: 8, 4: 8}.get(sub, 0) * run, imp
        if w >> 13 == 0b011:
            sub, idx = (w >> 9) & 0xf, w & 0x1ff
            if sub == 0:
                self.import_at[pos] = idx
                return pos + 4, idx + 1
            return pos + (4 if sub == 3 else 0), imp
        if w >> 12 == 0b1000:
            return pos + (w & 0xfff) + 1, imp
        return pos, imp

    # -- functions -----------------------------------------------------------
    def _find_functions(self):
        C = self.code
        found = {}
        for m in re.finditer(rb"\.?[A-Za-z_][\x21-\x7e]{1,250}", C):
            p = m.start()
            if p < 16:
                continue
            n = struct.unpack_from(">H", C, p - 2)[0]
            if n < 2 or n > len(m.group()):
                continue
            tboff = struct.unpack_from(">I", C, p - 6)[0]
            # the fixed part of the table starts 8 bytes after the zero word;
            # optional fields (parminfo, tb_offset, ...) sit between it and
            # the name, so try each plausible start.
            for k in range(6, 30, 4):
                t = p - k - 8
                if t < 4 or (t & 3):
                    continue
                if struct.unpack_from(">I", C, t - 4)[0] == 0 and C[t] == 0:
                    start = t - 4 - tboff
                    if 0 <= start < t - 4 and not start & 3:
                        found[start] = (t - 4, C[p:p + n].decode("latin-1").lstrip("."))
                    break
        self.funcs = sorted((s, e, n) for s, (e, n) in found.items())
        self.starts = [f[0] for f in self.funcs]
        self.by_start = {f[0]: f for f in self.funcs}

    def func_at(self, off):
        i = bisect.bisect_right(self.starts, off) - 1
        if i >= 0 and self.funcs[i][0] <= off < self.funcs[i][1] + 4:
            return self.funcs[i]
        return None

    def find(self, key):
        if re.fullmatch(r"0x[0-9a-fA-F]+", key):
            f = self.func_at(int(key, 16))
            return [f] if f else []
        exact = [f for f in self.funcs if f[2] == key]
        return exact or [f for f in self.funcs if f[2].startswith(key)]

    def word(self, off):
        if 0 <= off <= len(self.data) - 4:
            return struct.unpack_from(">I", self.data, off)[0]
        return None

    def toc_name(self, disp):
        """What the TOC slot at r2+disp names: an import, a function (through
        its transition vector) or a data offset."""
        if self.toc is None:
            return None
        slot = self.toc + disp
        if slot in self.import_at:
            return "import " + self.imports[self.import_at[slot]]
        w = self.word(slot)
        if w is None:
            return None
        if w in self.import_at:
            return "&import " + self.imports[self.import_at[w]]
        code = self.word(w)
        if code is not None and code in self.by_start:
            return self.by_start[code][2]
        if 0 <= w < len(self.data):
            m = re.match(rb"[\x20-\x7e\t\n]{3,60}", self.data[w:w + 60])
            if m and self.data[w + len(m.group()):w + len(m.group()) + 1] in (b"\0", b""):
                return "data+%#x %r" % (w, m.group().decode())
        return "data+%#x" % w

    def toc_const(self, disp, double):
        """The value an lfs/lfd from the TOC loads: the slot itself."""
        if self.toc is None:
            return None
        off = self.toc + disp
        if double and off <= len(self.data) - 8:
            return "%r" % struct.unpack_from(">d", self.data, off)[0]
        if not double and off <= len(self.data) - 4:
            return "%rf" % struct.unpack_from(">f", self.data, off)[0]
        return None

    def glue_target(self, off):
        """A cross-library call goes through a glue stub whose first
        instruction loads the import's slot from the TOC."""
        if 0 <= off <= len(self.code) - 4:
            w, = struct.unpack_from(">I", self.code, off)
            if w >> 16 == 0x8182:                            # lwz r12,d(r2)
                d = w & 0xffff
                d = d - 0x10000 if d & 0x8000 else d
                return self.toc_name(d)
        return None


class _Word:
    """An instruction word capstone cannot decode, shown as data."""
    def __init__(self, address, word):
        self.address, self.size = address, 4
        self.mnemonic, self.op_str = ".long", "%#010x" % word
        if word >> 26 == 63 and (word >> 1) & 0x3ff == 32:   # fcmpo
            self.mnemonic = "fcmpo"
            self.op_str = "cr%d, f%d, f%d" % (word >> 23 & 7, word >> 16 & 31, word >> 11 & 31)


def _disasm_all(md, code, base):
    """capstone stops at the first word it cannot decode (some of the
    CodeWarrior output, fcmpo among them); carry on past it instead."""
    off = 0
    while off < len(code):
        for ins in md.disasm(code[off:], base + off):
            yield ins
            off = ins.address + ins.size - base
        if off < len(code):
            yield _Word(base + off, struct.unpack_from(">I", code, off)[0])
            off += 4


def disasm(pef, f):
    try:
        from capstone import Cs, CS_ARCH_PPC, CS_MODE_32, CS_MODE_BIG_ENDIAN
    except ImportError:
        sys.exit("pef.py: disassembly needs capstone on PYTHONPATH (see the header)")
    md = Cs(CS_ARCH_PPC, CS_MODE_32 | CS_MODE_BIG_ENDIAN)
    s, e, n = f
    out = []
    for ins in _disasm_all(md, pef.code[s:e], s):
        note = ""
        if "(r2)" in ins.op_str:
            d = int(ins.op_str.split(",")[-1].split("(")[0].strip(), 0)
            if ins.mnemonic.startswith("lf"):
                t = pef.toc_const(d, ins.mnemonic.startswith("lfd"))
            else:
                t = pef.toc_name(d)
            if t:
                note = "  ; " + t
        elif ins.mnemonic in ("bl", "b") and ins.op_str.startswith("0x"):
            t = int(ins.op_str, 16)
            g = pef.glue_target(t)
            fn = pef.by_start.get(t)
            if fn and not g:
                g = pef.glue_target(fn[0])
            note = "  ; " + (g or (fn[2] if fn else "") or "")
        out.append("%06x  %-8s %s%s" % (ins.address, ins.mnemonic, ins.op_str, note.rstrip(" ;")))
    return out


def main():
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    cmd, path = sys.argv[1], sys.argv[2]
    pef = PEF(DEFAULT if path == "-" else path)
    arg = sys.argv[3] if len(sys.argv) > 3 else None
    if cmd == "info":
        for s in pef.sections:
            print("section %d kind %d size %#x unpacked %#x" % (s["index"], s["kind"], s["size"], s["unpacked"]))
        print("toc at data+%#x; %d functions named" % (pef.toc, len(pef.funcs)))
        for lib, first, cnt in pef.libs:
            print("import %-24s %d symbols" % (lib, cnt))
    elif cmd == "funcs":
        rx = re.compile(arg) if arg else None
        for s, e, n in pef.funcs:
            if not rx or rx.search(n):
                print("%06x %06x %6d %s" % (s, e, e - s, n))
    elif cmd == "imports":
        rx = re.compile(arg) if arg else None
        for k, n in enumerate(pef.imports):
            if not rx or rx.search(n):
                print("%-28s %s" % (pef.import_lib.get(k, "?"), n))
    elif cmd == "dis":
        for f in pef.find(arg) or sys.exit("no function %s" % arg):
            print("== %s  %#x..%#x" % (f[2], f[0], f[1]))
            print("\n".join(disasm(pef, f)))
    elif cmd == "refs":
        targets = {f[2] for f in pef.find(arg)}
        for f in pef.funcs:
            hits = [l for l in disasm(pef, f) if any(l.endswith("; " + t) for t in targets)]
            for h in hits:
                print("%-50s %s" % (f[2], h.split()[0]))
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
