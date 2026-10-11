#!/usr/bin/env python3
"""Measure src/engine/d3d's objects against the shipped d3d.ren, by hand.

`exematch` cannot do this job here. Most of what reproduces in d3d.ren is
tiny -- a six-byte `mov <global>,%eax; ret` is two unmasked bytes and four of
relocation -- and eight unmasked bytes is exematch's floor, below which a
short body matches at addresses it has nothing to do with. So the oracle
correctly refuses to place seventeen of the nineteen functions that actually
reproduce, and the scoreboard correctly reports what the oracle can place.

This checks the remaining question directly: for a symbol whose ADDRESS WE
ALREADY KNOW, are our bytes the shipped bytes? That is not a search, so the
floor does not apply, and it is the same footing as any hand-verified row.

The address comes from the symbol's own name -- `sub_1001c480` and friends --
which is why d3d functions are named that way. A handful of named functions
are mapped explicitly below.

Usage:
  d3dcheck.py [--obj=name] [--verbose]
"""
import glob
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from exematch import parse_obj_masked  # noqa: E402
from exedump import Pe  # noqa: E402

# functions whose names do not carry their address
NAMED = {
    "IsIn3D": 0x10032F20,
    "IsInOptimized2D": 0x10034C60,
    "Slot_c0": 0x10032E40,
    "ResetCounters": 0x1001EAB0,
    "GetSurfaceInfo": 0x10036920,
    "TransposeRotation": 0x1001E420,
    "Start3D": 0x10032E50,
    "End3D": 0x10032EA0,
    "CreateWorld": 0x10033160,
    "DestroyWorld": 0x10032C00,
    "SetOptimized2DColor": 0x10034EB0,
    "StartOptimized2D": 0x10034930,
    "EndOptimized2D": 0x10034B00,
    "SetOptimized2DBlend": 0x10034C70,
    "GetOptimized2DBlend": 0x10034ED0,
    "GetOptimized2DColor": 0x10034EF0,
    "CreateSurface": 0x100367B0,
    "DeleteSurface": 0x100368E0,
    "LockSurface": 0x10036950,
    "UnlockSurface": 0x100369A0,
    "OptimizeSurface": 0x10034580,
    "UnoptimizeSurface": 0x10034510,
    "LockScreen": 0x100369D0,
    "UnlockScreen": 0x10036A90,
    "BlitToScreen_DD": 0x10036BC0,
    "BlitToScreen": 0x10036CF0,
    "BlitWarpedToScreen": 0x10036DA0,
    "BlitFromScreen": 0x10036AE0,
    "GetSupportedModes": 0x100212D0,
    "FreeModeList": 0x100213E0,
    "RenderDLLSetup": 0x10021410,
    # ltmatrix.h's inline LTMatrix::Inverse: big0ae20.cpp's /Qip TU emits
    # its out-of-line IPO copy as ?Inverse@LTMatrix@@ (the real header's
    # name), and that copy is 0x1000ac10.
    "Inverse": 0x1000AC10,
    # Written under their engine names (w28_100256d0_qipni.cpp's RenderStruct
    # slot +0xb0, w6_10039730_qip.cpp's static /Qip texture upload), so the
    # address is in neither name.
    "d3d_RenderScene": 0x1002D1C0,
    "r_TransferTexture": 0x1003A080,
}

# Template members by their FULL mangled name: a stock header's instantiation
# (dynarray.h, object_bank.h) carries no address, and its leaf (`Term`,
# `AllocVoid`) is shared by every instantiation, so NAMED's leaf match cannot
# hold it.  Exact symbol -> shipped address, with the evidence.
SYMBOLS = {
    # ObjectBank<Node56ab4>'s vtable 0x100897d0 (+0x00 ??_G, +0x04 AllocVoid,
    # +0x08 FreeVoid, +0x0c Term), instantiated by d3dr5b.cpp (/Os /Qip).
    "??_G?$ObjectBank@UNode56ab4@@VNullCS@@@@UAEPAXI@Z": 0x10050CC0,
    "?AllocVoid@?$ObjectBank@UNode56ab4@@VNullCS@@@@UAEPAXXZ": 0x10050DA0,
    "?FreeVoid@?$ObjectBank@UNode56ab4@@VNullCS@@@@UAEXPAX@Z": 0x10050DC0,
    "?Term@?$ObjectBank@UNode56ab4@@VNullCS@@@@UAEXXZ": 0x10050DE0,
}

# Compiler-generated, no address of their own to check against.
# (A `qip_` prefix used to be skipped too: it fronted the deliberately wrong
# placeholder callees of the /Qip TUs. The last one, d3dqip.cpp's
# qip_Callee10040938, is now the real 0x10040930, so the skip is gone.)
SKIP = ("_$E",)


# Alignment padding icl emits at the end of a COMDAT. It is NOT 0x90 -- the
# whole reason d3d.ren stood out from every other module is that its compiler
# pads with multi-byte lea-nops, and a stripper that only knows 0x90/0xCC
# leaves them on and then compares them against whatever the shipped image
# happens to have between ITS functions. That is a comparison of two
# compilers' padding choices, not of two functions, and it reported ten
# differing bytes on functions that are identical.
PADS = [
    bytes.fromhex("8dbc2700000000"),   # lea 0x0(%edi,%eiz,1),%edi
    bytes.fromhex("8db42600000000"),   # lea 0x0(%esi,%eiz,1),%esi
    bytes.fromhex("8db600000000"),     # lea 0x0(%esi),%esi (6-byte form)
    bytes.fromhex("8dbf00000000"),     # lea 0x0(%edi),%edi (6-byte form;
                                       # ends a 12-byte pad after a static
                                       # initialiser, w57_1003b1c0.cpp)
    bytes.fromhex("8d742600"),         # lea 0x0(%esi,%eiz,1),%esi (short)
    bytes.fromhex("8d7600"),           # lea 0x0(%esi),%esi
    bytes.fromhex("8bf6"),             # mov %esi,%esi
    bytes.fromhex("8bff"),             # mov %edi,%edi
    b"\x90",
    b"\xcc",
]


def strip_pad(code):
    """Drop trailing alignment padding, whatever encoding it uses."""
    changed = True
    while changed:
        changed = False
        for p in PADS:
            if code.endswith(p):
                code = code[:-len(p)]
                changed = True
    return code


def main():
    opts = dict(a.split("=", 1) if "=" in a else (a, True)
                for a in sys.argv[1:] if a.startswith("--"))
    only = opts.get("--obj")

    pe = Pe("out/reference/d3d.ren")
    tva, tvs, trp = pe.secs[".text"]
    text = pe.d[trp:trp + tvs]

    hits, misses, unmapped = [], [], []
    for obj in sorted(glob.glob("src/engine/d3d/Release/*.obj")):
        # EXACT stem match, not a substring. `--obj=d3dr5d` silently
        # included d3dr5d_os.obj and made a split file look like it
        # scored its companion's matches too; `--obj=d3d` once matched
        # every file in the directory. Pass the stem without .obj.
        if only and only != os.path.basename(obj)[:-4]:
            continue
        try:
            fns = parse_obj_masked(open(obj, "rb").read())
        except Exception:
            continue
        for sym, (code, mask) in fns.items():
            parts = re.sub(r"^\?", "", sym).split("@")
            leaf = parts[0]
            # The address may be in the CLASS name rather than the method
            # name: a member written `Arr10021ad0::Insert2` mangles to
            # `?Insert2@Arr10021ad0@@...`, and looking only at the leaf
            # dropped it -- nine of one batch's symbols went unmeasured that
            # way, reported as "no address in the name" while the address was
            # sitting one component along. Leaf first, then the class.
            m = re.search(r"(1[0-9a-f]{7})", leaf)
            if not m and len(parts) > 1:
                m = re.search(r"(1[0-9a-f]{7})", parts[1])
            a = int(m.group(1), 16) if m else SYMBOLS.get(sym) or \
                next((v for k, v in sorted(NAMED.items(),
                                           key=lambda kv: -len(kv[0]))
                      if k in leaf), None)
            # A /Qip function with EXTERNAL linkage compiles to two symbols
            # in one section: `X` (the stack entry, `mov N(%esp),%reg` per
            # argument) falling into `X$` (the register entry). The name
            # carries the STACK entry's address, so `X` maps by its name and
            # `X$` sits the stack entry's length above it -- measuring it at
            # the named address compared the register entry against the
            # stack entry's bytes.
            if a is not None and leaf.endswith("$"):
                base = sym.replace(leaf, leaf[:-1], 1)
                if base in fns:
                    a += len(fns[base][0])
            # icl's `X$$1` is the body behind X, the 14-byte stack-aligning
            # entry (`push %ebx ; mov %esp,%ebx ; sub $8 ; and $-16 ; add $8 ;
            # jmp`) that falls into the body's own `push %ebx ; mov %esp,%ebx`
            # -- so it too sits X's length above X's address.
            if a is not None and leaf.endswith("$$1"):
                base = sym.replace(leaf, leaf[:-3], 1)
                if base in fns:
                    a += len(strip_pad(fns[base][0]))
            if a is None:
                if not leaf.startswith(SKIP):
                    unmapped.append(leaf)
                continue
            body = strip_pad(code)
            # An empty body is not a match. A symbol whose COMDAT is nothing
            # but padding strips to zero bytes, compares equal to anything,
            # and was being counted as byte-identical -- one function of
            # false credit, which is exactly what this oracle exists to stop.
            if not body:
                continue
            off = a - tva
            if off < 0 or off + len(body) > len(text):
                misses.append((a, len(body), -1, leaf))
                continue
            d = sum(1 for i in range(len(body))
                    if i not in mask and body[i] != text[off + i])
            (hits if d == 0 else misses).append((a, len(body), d, leaf))

    hits.sort()
    misses.sort()
    print(f"  {len(hits)} byte-identical, {sum(h[1] for h in hits)} bytes")
    if opts.get("--verbose"):
        for a, n, _d, leaf in hits:
            print(f"    {a:#010x} {n:>5}b  {leaf}")
    if misses:
        print(f"\n  {len(misses)} not identical:")
        for a, n, d, leaf in misses:
            where = "outside .text" if d < 0 else f"{d} byte(s) differ"
            print(f"    {a:#010x} {n:>5}b  {where:<18} {leaf}")
    if unmapped:
        print(f"\n  {len(unmapped)} symbol(s) with no address in the name "
              f"(add to NAMED or rename sub_<addr>):")
        for leaf in sorted(set(unmapped)):
            print(f"    {leaf}")


if __name__ == "__main__":
    main()
