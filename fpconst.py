#!/usr/bin/env python3
"""Check the constants that byte-matching cannot see.

exematch masks every byte a relocation covers, which is what lets an
unrelocated object be compared against a linked image at all. The cost is
that anything reached *through* a relocation is invisible to it: a float
literal lives in .rdata and is loaded by address, so `+= 0.001f` and
`+= 1.0f` produce identical maskable bytes and the oracle calls both a
match. One such error sat in CNetMgr::AddRecvBytes for months and was
found only by accident.

This closes that hole. For every function of ours that exematch places in
the image, it walks the object's relocations, and where one points at a
read-only constant in our object it reads the matching address out of the
shipped image and compares the constant itself.

A relocation counts only when the instruction that owns it, decoded from
the function's entry (x86len.py), is an x87 read of a float or double
from memory. Reading the opcode from the two bytes in front of the field
mistook the `dd d8` of an `fstp st(0)` for one, ahead of a `mov esi,
imm32`. Exits 1 on a mismatch.

Usage:
  fpconst.py <image> <obj|dir>...            e.g.
  fpconst.py out/reference/server.dll src/engine/server/Release
  fpconst.py --targets      every built image target in progress/targets.py
                            (its link's objects): `make progress` runs this
"""
import glob
import os
import struct
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from exematch import parse_obj_masked, find  # noqa: E402
from exedump import Pe  # noqa: E402
from x86len import walk  # noqa: E402
from libextract import extract  # noqa: E402

DIR32 = 6


def x87_width(ins):
    """The bytes an x87 instruction reads from memory as a float operand,
    or 0 if it reads none (a store, a control word, an integer, a register).

    D8 /r: fadd..fdivr m32real;  D9 /0: fld m32real
    DC /r: fadd..fdivr m64real;  DD /0: fld m64real
    """
    if ins.mod is None or ins.mod == 3:
        return 0
    if ins.op == 0xD8:
        return 4
    if ins.op == 0xDC:
        return 8
    if ins.op == 0xD9 and ins.reg == 0:
        return 4
    if ins.op == 0xDD and ins.reg == 0:
        return 8
    return 0


def sections(data):
    """[(name, vsize, rawsize, rawptr)] plus the symbol table, from a COFF."""
    nsec, symoff, nsyms, optsz = struct.unpack_from("<HxxxxIIH", data, 2)
    secs = []
    for i in range(nsec):
        s = 20 + optsz + i * 40
        name = data[s:s + 8].rstrip(b"\0").decode("latin-1")
        vsize, _va, rawsz, rawp = struct.unpack_from("<IIII", data, s + 8)
        relp, _lnp, nrel = struct.unpack_from("<IIH", data, s + 24)
        flags = struct.unpack_from("<I", data, s + 36)[0]
        secs.append((name, rawsz, rawp, relp, nrel, flags))
    return secs, symoff, nsyms


def const_at(data, secs, symoff, nsyms, symidx, addend, width):
    """The `width` bytes our object holds for relocation target symidx."""
    e = symoff + symidx * 18
    if e + 18 > len(data):
        return None
    value, secnum = struct.unpack_from("<Ih", data, e + 8)
    if secnum <= 0 or secnum > len(secs):
        return None
    name, rawsz, rawp, _rp, _nr, flags = secs[secnum - 1]
    if not flags & 0x40:            # IMAGE_SCN_CNT_INITIALIZED_DATA
        return None
    if flags & 0x80000000:          # writable: not a literal pool
        return None
    off = rawp + value + addend
    if off < rawp or off + width > rawp + rawsz:
        return None
    return data[off:off + width]


def func_syms(data, secs, symoff, nsyms):
    """[(name, section_index, offset_in_section)] for defined code symbols.

    Needed because a relocation's address is relative to its own section, and
    a function is only the whole of its section under /Gy. Attributing every
    section's relocations to every function -- which the first version of this
    did -- cross-attributes constants between unrelated functions and invents
    mismatches. So each symbol is tied to the section it is defined in.
    """
    strtab = data[symoff + nsyms * 18:]
    out, i = [], 0
    while i < nsyms:
        e = symoff + i * 18
        raw = data[e:e + 8]
        if raw[:4] == b"\0\0\0\0":
            off = struct.unpack_from("<I", raw, 4)[0]
            name = strtab[off:strtab.find(b"\0", off)].decode("latin-1")
        else:
            name = raw.rstrip(b"\0").decode("latin-1")
        value, secnum, _typ, storage, naux = struct.unpack_from("<IhHBB", data, e + 8)
        if 0 < secnum <= len(secs) and storage in (2, 6):
            if secs[secnum - 1][5] & 0x20:      # code section
                out.append((name, secnum, value))
        i += 1 + naux
    return out


def check(image, objs, out=sys.stdout):
    """(operands checked, mismatched, unknown) for our objects in image."""
    pe = Pe(image)
    tva, tvs, trp = pe.secs[".text"]
    text = pe.d[trp:trp + tvs]

    def img_read(va, n):
        for _nm, (sva, svs, srp) in pe.secs.items():
            if sva <= va < sva + svs:
                o = srp + (va - sva)
                return pe.d[o:o + n]
        return None

    checked = bad = unknown = 0
    for path in objs:
        if isinstance(path, tuple):         # (name, bytes): a library member
            path, data = path
        else:
            data = open(path, "rb").read()
        try:
            fns = parse_obj_masked(data)
            secs, symoff, nsyms = sections(data)
        except Exception:
            continue
        home = {n: (si, val) for n, si, val in func_syms(data, secs, symoff, nsyms)}
        relbysec = {}
        for si, (name, rawsz, rawp, relp, nrel, flags) in enumerate(secs, 1):
            if not flags & 0x20 or not relp:
                continue
            relbysec[si] = [struct.unpack_from("<IIH", data, relp + i * 10)
                            for i in range(nrel)]

        for sym, (code, mask) in fns.items():
            if len(code) - len(mask) < 8 or sym not in home:
                continue
            si, symval = home[sym]
            hits = list(find(text, tva, code, mask))
            if len(hits) != 1:
                continue                     # ambiguous or absent: not ours to judge
            base = hits[0]
            # each relocation is judged by the instruction that owns it: the
            # function is decoded from its entry, and only a DIR32 that is
            # the whole 4-byte displacement of an x87 memory read counts.
            # The relocations past the first byte the decoder cannot read
            # are counted as unknown, not passed.
            owner = {}
            decoded_to = 0
            for ins in walk(code):
                decoded_to = ins.off + ins.length
                if ins.disp_off is not None and ins.disp_len == 4:
                    owner[ins.disp_off] = ins
            for vaddr, symidx, typ in relbysec.get(si, []):
                fn_off = vaddr - symval
                if not (0 <= fn_off < len(code)) or fn_off not in mask:
                    continue
                ins = owner.get(fn_off)
                if ins is None:
                    if fn_off >= decoded_to:
                        unknown += 1
                    continue
                width = x87_width(ins)
                if not width or typ != DIR32:
                    continue
                # a DIR32's addend is the field's own content in the object
                addend = struct.unpack_from("<i", code, fn_off)[0]
                ours = const_at(data, secs, symoff, nsyms, symidx, addend, width)
                if ours is None:
                    continue
                va = struct.unpack_from("<I", text, base - tva + fn_off)[0]
                theirs = img_read(va, width)
                if not theirs or len(theirs) != width:
                    continue
                checked += 1
                if ours != theirs:
                    bad += 1
                    f = "<f" if width == 4 else "<d"
                    print(f"MISMATCH {os.path.basename(path)} {sym}", file=out)
                    print(f"    +0x{fn_off:x} operand at 0x{va:08x}: "
                          f"ours {struct.unpack(f, ours)[0]!r} "
                          f"ship {struct.unpack(f, theirs)[0]!r}", file=out)
    return checked, bad, unknown


def summary(checked, bad, unknown):
    return (f"{checked} floating-point operand(s) behind masked relocations "
            f"checked, {bad} mismatched"
            + (f"; {unknown} relocation(s) past code the decoder could not "
               f"read, unchecked" if unknown else ""))


def check_targets():
    """Every built image target of progress/targets.py, its link's objects
    against its shipped image: what `make progress` runs."""
    import tgt as tgt_mod
    os.chdir(tgt_mod.REPO)
    bad_total = 0
    for t in tgt_mod.cfg.TARGETS:
        if t.get("kind") != "image" or not tgt_mod.built(t):
            continue
        objs = []
        for o in tgt_mod.progress.image_objs(t, tgt_mod.built(t)):
            if o.lower().endswith(".obj"):
                objs.append(o)
            elif o.lower().endswith(".lib"):
                # a library of ours is checked member by member
                with tempfile.TemporaryDirectory() as td:
                    extract(o, td)
                    objs += [(os.path.basename(m), open(m, "rb").read())
                             for m in sorted(glob.glob(td + "/*"))]
        if not objs:
            continue
        shipped = t["shipped"]
        if t.get("readable") and os.path.exists(t["readable"]):
            shipped = t["readable"]
        checked, bad, unknown = check(shipped, objs)
        bad_total += bad
        print(f"  {t['label']}: " + summary(checked, bad, unknown))
    return 1 if bad_total else 0


def main():
    if sys.argv[1:] == ["--targets"]:
        return check_targets()
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    objs = []
    for a in sys.argv[2:]:
        objs += sorted(glob.glob(os.path.join(a, "*.obj"))) if os.path.isdir(a) else [a]
    checked, bad, unknown = check(sys.argv[1], objs)
    print("\n" + summary(checked, bad, unknown))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
