#!/usr/bin/env python3
"""Rank d3d.ren's remaining functions by what it is worth doing next.

THE COMPILER IS KNOWN. d3d.ren was built by Intel C/C++ 4.0 at /O2 (see
toolchains/COMPILERS.md), and it reproduces byte-for-byte including the
compiler's own alignment padding. Everything this file used to treat as a
blocker -- the frame pointer, `xor %eax,%eax; inc %eax`, the dummy stack
slot, the lea-nop padding, arguments passed in registers -- is simply what
icl emits, and our build emits it too. There is no "cannot be verified"
class any more.

So the ranking is now ordinary leverage, in this order:

  1. a NAMED RenderStruct slot -- the name and signature are already known
     from the client side, so the decode starts half done;
  2. how many callers depend on it -- a function 76 sites reach is worth
     more than a leaf, and getting it wrong is worth less;
  3. size, smallest first within a band, because small functions verify
     fast and calibrate the source style for the big ones.

What is NOT reachable from C source, and how it is told apart, is easy to get
wrong, and the ground truth is `d3dcheck.py --verbose`: any
rule that puts one of the byte-identical functions out of reach is wrong.

  * icl passes a function's leading arguments in %eax/%edx/%ecx to callers
    in the SAME translation unit. Such a function has two entries: the
    stack-ABI entry loads the arguments off the stack (`mov 4(%esp),%eax`,
    up to three of them), and falls through into the REGISTER ENTRY, which
    same-TU callers reach by calling PAST those loads with the registers
    already set. 0x10039bd0 is the shape: three loads, then the body at
    0x10039bdc; 46 callers use the former and three the latter.

    The whole thing, from the loads, is one ordinary C function -- our own
    build of sub_10079f30 emits the same twelve-byte load run in front of
    its body -- so the row that is worth working on is the FRONT. The
    enumeration splits it, and an earlier cut of this tool then dropped the
    front as "a shim" and put the body in Z, which hid the function twice
    over. Now the pair is merged into one row at the front address.

  * a REGISTER ENTRY on its own -- a start that reads %eax or %edx before
    anything has written them, or that a call site hands them to -- is not
    a function that can be written by itself. It is the far half of a pair
    (Z, with the front's address in the row), or, when nothing sits in
    front (0x1007b580, behind a bare `ret`), a static that icl gave a
    register-only ABI.

    THIS IS REPRODUCIBLE, and the construct is now known: `/Qip` --
    Intel's interprocedural optimisation -- plus internal linkage. Measured
    directly. A recursive `static` function at `/O2 /Qip` is emitted with
    NO stack entry, opening `mov %eax,%ebx; test %ecx,%ecx; mov %edx,%ebp`,
    and its callers set %eax/%edx/%ecx before the `call`. The same function
    with EXTERNAL linkage gets both halves, as two symbols: the 12-byte
    stack entry under its own name, and the register entry under the same
    name with a `$` appended. That is the shipped two-entry shape exactly.

    So the requirement is structural rather than impossible: the callee and
    its callers must be in ONE of our translation units, compiled `/Qip`.
    `/Qip` is near-neutral on files that have no intra-TU calls (measured:
    d3dr4b 29 and 29, d3dr4a 28 and 28, d3dr5a 13 and 12), so it is a
    per-file flag like `/Os` and belongs in the same measured list.

    The earlier test for this looked for ANY `mov` out of %eax/%ecx/%edx in
    the first 28 bytes, or a shim in front. Checked against the ground
    truth it put 66 of the 176 byte-identical functions in Z: `mov
    %ecx,%ebx` and `mov (%ecx),%eax` are how every __thiscall member
    starts, and 0x10035bb4, 0x10079f84 and 0x1007dec4 reproduce sitting
    right behind a `mov 4(%esp),%eax` whose load their bodies never use.
    That test is gone.

  * a REGISTER-ABI CALLER (tier Y): a function that is ordinary in itself
    but makes a `call` whose arguments travel in %eax/%edx -- written just
    before the call and never stored, pushed or otherwise consumed. No C
    call expression produces that: an optimising compiler never writes a
    caller-saved register dead across a call unless the callee reads it.
    0x1007b880 (`mov $1,%eax ; call 0x1007b580`), 0x10026670 (`mov
    $imm,%eax ; lea ..,%edx ; call 0x100265a8` with the reserved slots
    never written) and 0x10039f90 are the confirmed cases; eight of one
    25-row batch turned out to be these.

    Y is NOT Z. The body of a Y row is real C and its entry is a real ABI;
    what cannot be written is one call expression, and it becomes
    writable the moment its callee is in the same translation unit and
    icl picks the same registers -- which nobody has yet made happen, so Y
    sorts after D and ahead of Z, and the row says which call blocks it.
    A Z row is not a function in its own right at all.

    Only %eax and %edx count as evidence. %ecx is `this` under __thiscall,
    so `mov $obj,%ecx ; call` is the most ordinary call in the image, and
    the register entries that take a third argument in %ecx always take
    the first two in %eax/%edx as well.

  * the calling-convention SHIM rows themselves -- an enumerated row that
    is nothing but the loads -- are folded into the merged row when a body
    follows, and dropped when nothing does;
  * the CRT above 0x1007fa10, which is Microsoft's libc pulled in by the
    linker and not this project's code.

The register analysis is done on real instructions -- one objdump pass over
.text -- rather than byte patterns. A row's body is cut at the first `ret`
that padding follows, because the enumeration misses starts (0x10068910 is
not in it) and the CSV size then runs into the neighbour: 0x100688e0 is 38
bytes, its row says 367, and the neighbour's calls are not its calls.

Usage:
  d3dwork.py [--top=50] [--csv=out.csv]
"""
import csv
import os
import re
import struct
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from exedump import Pe  # noqa: E402

IMG = "out/reference/d3d.ren"
CSV = "out/reference/d3d-functions.csv"   # build/reference.sh
CRT_LO = 0x1007FA10          # MSVC libc from here up
SETUP = 0x10021410           # RenderDLLSetup

FINGERPRINTS = [
    (bytes.fromhex("558bec"), "frame"),
    (bytes.fromhex("33c040"), "xor/inc"),

    (bytes.fromhex("c70424"), "mov-arg"),
    (bytes.fromhex("8d7600"), "gas-pad"),
    (bytes.fromhex("8dbc2700000000"), "gas-pad"),
    (bytes.fromhex("8db42600000000"), "gas-pad"),
    # `add $-8,%esp` / `add $-12,%esp` where cl writes `sub`. Same
    # instruction, different encoding, and cl never picks this one.
    (bytes.fromhex("83c4f8"), "add-neg-esp"),
    (bytes.fromhex("83c4f4"), "add-neg-esp"),
    (bytes.fromhex("83c4ec"), "add-neg-esp"),
]

# x87 arithmetic. The unknown compiler accumulates n-ary float expressions
# from the LAST term backwards, and cl cannot re-associate float adds, so
# anything doing real FP maths is out of reach regardless of shape.
X87 = set(range(0xD8, 0xE0))

# What icl puts between functions: 0x90, 0xCC, and the multi-byte lea-nops
# it prefers. A `ret` followed by one of these is the end of a function.
PADS = [bytes.fromhex(h) for h in (
    "8dbc2700000000", "8db42600000000", "8d742600", "8d7600",
    "8db600000000", "8dbf00000000", "8bf6", "8bff", "90", "cc")]


def is_shim(body):
    """The stack-ABI entry of a register-passing function: one to three
    `mov N(%esp),reg` loads and nothing else.

    icl loads the leading arguments into %eax, %edx, %ecx before the body
    so that callers in the same translation unit can enter PAST the loads
    with the registers already set. Where such a caller exists the
    enumeration splits the function at the register entry and the loads
    become a 4-, 8- or 12-byte row of their own -- 0x10039bd0 is the
    three-argument case, body at +12. Those rows are folded back onto the
    body in main(); this only recognises the shape.

    A fourth unit is an x87 load of a stack argument. 0x1006b300 is
    `mov 0x4(%esp),%eax ; mov 0x8(%esp),%edx ; mov 0xc(%esp),%ecx ;
    flds 0x28(%esp)` and its register entry at 0x1006b310 opens by storing
    st(0) into its frame. Refusing the x87 unit hid that pair and put
    0x1006b310 in Z as a register-only ABI it is not. It is the only such
    front in the image: widening to 16 bytes and three x87 opcodes changes
    exactly one row, and none of the byte-identical functions.
    """
    if not body or len(body) > 16 or len(body) % 4:
        return False
    for i in range(0, len(body), 4):
        u = body[i:i + 4]
        if u[2] != 0x24:
            return False
        if u[0] == 0x8B and (u[1] & 0xC7) == 0x44:
            continue                       # mov N(%esp),r32
        if u[0] in (0xD9, 0xDD, 0xDB) and u[1] == 0x44:
            continue                       # flds/fldl/fildl N(%esp)
        return False
    return True


def front_of(text, tva, a, allstarts):
    """The stack-ABI entry in front of register entry `a`, or None.

    Up to three loads, and the run must START somewhere a function can
    start: an enumerated row, a 16-byte boundary (icl aligns every function
    to 16, and all eleven fronts checked by hand sit on one), or right
    behind padding or a `ret`. A lone `8b 44 24 xx` at the tail of the
    previous function is not a front -- that chance window is what made an
    earlier cut label ordinary __thiscall callers as register-ABI.
    """
    for k in (16, 12, 8, 4):
        f = a - k
        if f - tva < 0 or not is_shim(text[f - tva:a - tva]):
            continue
        prev = text[f - tva - 1] if f - tva > 0 else 0
        if f in allstarts or f % 16 == 0 or prev in (0x90, 0xCC, 0xC3) \
                or any(text[f - tva - len(p):f - tva] == p for p in PADS):
            return f
    return None


# ---------------------------------------------------------------------------
# Instruction-level view. The register-ABI questions -- "is %eax live into
# this call", "does this body read %edx before writing it" -- cannot be
# answered from byte patterns (the first two attempts tried), so .text is
# disassembled once and each instruction reduced to the registers it reads
# and writes among %eax, %ecx, %edx.

REG = {"eax": "eax", "ax": "eax", "al": "eax", "ah": "eax",
       "ecx": "ecx", "cx": "ecx", "cl": "ecx", "ch": "ecx",
       "edx": "edx", "dx": "edx", "dl": "edx", "dh": "edx"}
_REGRE = re.compile(r"%([a-z]{2,3})")
_INSRE = re.compile(r"^\s*([0-9a-f]+):\t([0-9a-f ]+?)\s*\t(\S+)\s*(.*)$")

# Two-operand groups (AT&T order: source first, destination last).
MOVLIKE = {"mov", "movzb", "movzw", "movsb", "movsw", "movzx", "movsx",
           "lea", "cvtsi2ss", "cvtsi2sd", "cvttss2si", "cvttsd2si",
           "movd", "bsf", "bsr"}
RWLIKE = {"add", "sub", "and", "or", "xor", "adc", "sbb", "shl", "shr",
          "sar", "sal", "rol", "ror", "rcl", "rcr", "shld", "shrd", "imul",
          "cmovs", "cmovne", "cmove", "cmovl", "cmovg", "cmovle", "cmovge",
          "cmova", "cmovb", "cmovae", "cmovbe", "xchg", "bts", "btr", "btc"}
READONLY = {"cmp", "test", "bt"}
ONE_RW = {"inc", "dec", "neg", "not"}
STRING = {"movs", "stos", "scas", "lods", "cmps"}


def _regs(op):
    return {REG[r] for r in _REGRE.findall(op) if r in REG}


def _split(ops):
    out, depth, cur = [], 0, ""
    for ch in ops:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            out.append(cur)
            cur = ""
        else:
            cur += ch
    if cur:
        out.append(cur)
    return [o.strip() for o in out]


def _isreg(op):
    return op.startswith("%") and "(" not in op and ":" not in op


def _base(mnem):
    """Strip the AT&T size suffix: movl -> mov, incl -> inc, movzbl -> movzb."""
    if mnem in MOVLIKE | RWLIKE | READONLY | ONE_RW or mnem.startswith("f"):
        return mnem
    if mnem[-1] in "lwbq" and mnem[:-1] in (
            MOVLIKE | RWLIKE | READONLY | ONE_RW | STRING | {"push", "pop",
            "div", "idiv", "mul", "set", "call", "jmp", "cmov"}):
        return mnem[:-1]
    return mnem


def effects(mnem, ops):
    """(reads, writes, explicit) over {eax, ecx, edx} for one instruction.

    `explicit` is the subset of `writes` where the register is the
    instruction's named destination -- a `mov`, `lea`, `xor %r,%r`, an
    arithmetic result. A register that is merely a side effect (`div`
    leaves the remainder in %edx, `cltd` fills it, `fnstsw %ax`) is dead
    for free, and a dead side effect in front of a call is ordinary code:
    `xor %edx,%edx ; div %ecx ; mov %eax,0x40(%ebx) ; call 0x1007b8d0` at
    0x100219e0 was the false positive that forced the distinction.

    Memory operands read their base and index. The x87 side reads whatever
    addresses its operand and writes nothing, except `fnstsw %ax`. Anything
    not modelled is taken to READ every register it names and write none:
    that direction only ever hides a register argument, never invents one.
    `ret` reads nothing here: the return value is not live-in evidence.
    """
    m = _base(mnem)
    o = _split(ops) if ops else []
    named = set().union(*[_regs(x) for x in o]) if o else set()
    memregs = set().union(*[_regs(x) for x in o if not _isreg(x)]) if o else set()
    dst = _regs(o[-1]) if o and _isreg(o[-1]) else set()
    none = set()
    if m.startswith("f"):
        if m in ("fnstsw", "fstsw") and dst:
            return none, dst, none
        return named, none, none
    if m in ("nop", "leave", "ret", "cltd", "cdq", "sahf", "lahf", "cwtl",
             "cbtw"):
        return {"cltd": ({"eax"}, {"edx"}, none), "cdq": ({"eax"}, {"edx"}, none),
                "sahf": ({"eax"}, none, none), "lahf": (none, {"eax"}, none),
                "cwtl": ({"eax"}, {"eax"}, none), "cbtw": ({"eax"}, {"eax"}, none),
                }.get(m, (none, none, none))
    if m in ("rep", "repz", "repnz", "lock"):
        parts = ops.split(None, 1)
        r, w, _ = effects(parts[0], parts[1] if len(parts) > 1 else "")
        return r | {"ecx"}, w | {"ecx"}, none
    if m in STRING:
        # movs/cmps move memory to memory; only stos/scas read %eax and
        # only lods writes it. `rep movsl` is how a struct return is copied
        # out (0x100156c7 in LTVec::operator*), and reading %eax there put
        # three byte-identical functions in Z.
        return ({"eax", "ecx"} if m in ("stos", "scas") else {"ecx"}), \
            ({"eax"} if m == "lods" else none), none
    if m in ("call", "jmp") or m.startswith("j"):
        return named, none, none          # indirect targets read their regs
    if m.startswith("set"):
        return memregs, dst, none         # writes one byte of it
    if m in ("div", "idiv", "mul") or (m == "imul" and len(o) == 1):
        return named | {"eax", "edx"}, {"eax", "edx"}, none
    if m == "push":
        return named, none, none
    if m == "pop":
        return memregs, dst, none
    if m in ONE_RW and len(o) == 1:
        return named, dst, dst
    if len(o) == 2:
        src = o[0]
        if m in MOVLIKE:
            return _regs(src) | (memregs - _regs(src) if dst else memregs), dst, dst
        if m in READONLY:
            return named, none, none
        if m in RWLIKE:
            if m in ("xor", "sub") and src == o[1] and dst:
                return none, dst, dst              # zeroing idiom
            if m == "xchg":
                both = dst | (_regs(src) if _isreg(src) else none)
                return named, both, none
            return named, dst, dst
    if len(o) == 3 and m == "imul":
        return _regs(o[1]) | memregs, dst, dst
    if len(o) == 1 and m in RWLIKE:          # `shl %eax` = by one
        return named, dst, dst
    return named, none, none


class Code:
    """Every instruction in .text below the CRT, from one objdump pass.

    `ins[addr] = (length, mnemonic, operands)`. A linear sweep can lose
    sync on inline data; a start that is not on an instruction boundary is
    re-disassembled by itself in `at()`.
    """

    def __init__(self, text, tva, hi):
        self.text, self.tva = text, tva
        self.ins = {}
        self._load(0, hi - tva)

    def _load(self, lo, hi):
        with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as f:
            f.write(self.text[lo:hi])
            tmp = f.name
        out = subprocess.run(
            ["objdump", "-D", "-b", "binary", "-m", "i386",
             f"--adjust-vma=0x{self.tva + lo:x}", tmp],
            capture_output=True, text=True).stdout
        os.unlink(tmp)
        rows = []
        for line in out.splitlines():
            m = _INSRE.match(line)
            if m and not m.group(3).startswith("."):
                rows.append((int(m.group(1), 16), m.group(3), m.group(4)))
        for i, (a, mn, op) in enumerate(rows):
            nxt = rows[i + 1][0] if i + 1 < len(rows) else self.tva + hi
            self.ins[a] = (nxt - a, mn, op.split("<")[0].strip())

    def at(self, a, n):
        if a not in self.ins:
            self._load(a - self.tva, a - self.tva + n)
        return self.ins.get(a)


def scan(code, text, tva, a, n):
    """Decode one function body and answer the register-ABI questions.

    Returns (end, sites, live_in):
      end     -- where the function really stops: the first `ret` that
                 padding follows and no earlier branch jumps over;
      sites   -- [(call addr, target, regs)] for every direct call that
                 %eax or %edx is live into: EXPLICITLY written since the
                 block began and not consumed. The block begins at the
                 function start, at any branch target, and after every
                 call/jmp/ret; a branch target resets the state (the other
                 path may have set the register), which only loses
                 evidence. A site is only evidence once load() has checked
                 that the callee reads the register -- icl keeps a value
                 live ACROSS a call to a same-TU leaf it knows preserves it;
      live_in -- registers among %eax/%edx that some path from the entry
                 reads before writing, stopping at the first call (an
                 incoming register argument has to be consumed or saved
                 before anything clobbers it) or return.
    """
    lo, hi = a, a + n
    seq, index = [], {}
    pos = a
    while pos < hi:
        r = code.at(pos, hi - pos)
        if not r:
            break
        ln, mn, op = r
        index[pos] = len(seq)
        seq.append((pos, ln, mn, op))
        pos += ln

    def _target(mn, op):
        if (mn.startswith("j") or mn == "call") and re.fullmatch(r"0x[0-9a-f]+", op or ""):
            t = int(op, 16)
            if lo <= t < hi:
                return t
        return None

    # the real end: a `ret` that padding follows, unless a branch from
    # before it jumps over that padding (then the code after is still ours)
    end, reach, targets = hi, a, set()
    for pos, ln, mn, op in seq:
        t = _target(mn, op)
        if t is not None:
            targets.add(t)
            reach = max(reach, t)
        if mn == "ret":
            after = pos + ln
            if (after >= hi or any(text.startswith(p, after - tva) for p in PADS)) \
                    and reach < after:
                end = after
                break

    # live-in: every path from the entry, until a call or return
    live_in = set()
    work = [(a, frozenset(("eax", "edx")))]
    seen = set()
    while work and len(seen) < 4000:
        pos, state = work.pop()
        if pos >= end or pos not in index or (pos, state) in seen or not state:
            continue
        seen.add((pos, state))
        _, ln, mn, op = seq[index[pos]]
        reads, writes, _ = effects(mn, op)
        live_in |= reads & state
        state = state - frozenset(writes)
        if mn == "ret" or mn == "call":
            continue
        t = _target(mn, op)
        if mn == "jmp":
            if t is not None:
                work.append((t, state))
            continue
        if t is not None:
            work.append((t, state))
        work.append((pos + ln, state))

    pending = {}
    sites = []
    for pos, ln, mn, op in seq:
        if pos >= end:
            break
        if pos in targets:
            pending = {}
        reads, writes, explicit = effects(mn, op)
        for r in reads:
            pending[r] = False
        if mn == "call" and re.fullmatch(r"0x[0-9a-f]+", op or ""):
            t = int(op, 16)
            live = {r for r in ("eax", "edx") if pending.get(r)}
            if live and tva <= t < CRT_LO:
                sites.append((pos, t, live))
        for r in writes:
            pending[r] = r in explicit
        if mn in ("call", "ret", "jmp"):
            pending = {}
    return end, sites, live_in


def slot_map(text, tva):
    """RenderStruct slot -> d3d.ren address, read out of RenderDLLSetup."""
    out, i, end = {}, SETUP - tva, SETUP - tva + 0x180
    while i < end:
        if text[i] == 0xC7 and text[i + 1] == 0x40:
            out[text[i + 2]] = struct.unpack_from("<I", text, i + 3)[0]
            i += 7
        elif text[i] == 0xC7 and text[i + 1] == 0x80:
            out[struct.unpack_from("<I", text, i + 2)[0]] = \
                struct.unpack_from("<I", text, i + 6)[0]
            i += 10
        elif text[i] == 0xC3:
            break
        else:
            i += 1
    return out


NAMES = {
    0x6c: "Init", 0x70: "Term", 0x74: "BindTexture", 0x78: "UnbindTexture",
    0x7c: "CreateWorld", 0x80: "DestroyWorld", 0x84: "ClearScreen",
    0x88: "Start3D", 0x8c: "End3D", 0x90: "IsIn3D",
    0x94: "StartOptimized2D", 0x98: "EndOptimized2D", 0x9c: "IsInOptimized2D",
    0xa0: "SetOptimized2DBlend", 0xa4: "GetOptimized2DBlend",
    0xa8: "SetOptimized2DColor", 0xac: "GetOptimized2DColor",
    0xb0: "RenderScene", 0xb8: "GetRenderObject", 0xbc: "FlipScreen",
    0xc8: "CreateSurface", 0xcc: "DeleteSurface", 0xd0: "GetSurfaceInfo",
    0xd4: "LockSurface", 0xd8: "UnlockSurface", 0xdc: "OptimizeSurface",
    0xe0: "UnoptimizeSurface", 0xe4: "LockScreen", 0xe8: "UnlockScreen",
    0xec: "BlitToScreen", 0xf0: "BlitWarpedToScreen", 0xf4: "ScreenShot",
    0xfc: "BlitFromScreen",
}


def load():
    """Image, enumeration, and the register-ABI facts for every row.

    Returns (text, tva, sizes, callers, entries, fronts, scans) where
      sizes[a]     -- CSV size clamped to the next enumerated start;
      callers[t]   -- direct-call count onto t;
      entries      -- {register entry: (front or None, evidence)} -- every
                      start that is the target of a register-passing call or
                      reads %eax/%edx live-in;
      scans[a]     -- (end, sites, live_in) from scan() for every row.
    Everything the ground-truth check (see check_ground_truth) needs is
    computed here, for ALL rows, claimed or not.
    """
    pe = Pe(IMG)
    tva, tvs, trp = pe.secs[".text"]
    text = pe.d[trp:trp + tvs]
    allrows = {int(r["addr"], 16): int(r["size"]) for r in csv.DictReader(open(CSV))}
    allstarts = set(allrows)
    _ss = sorted(allstarts)
    nextstart = {v: _ss[i + 1] for i, v in enumerate(_ss[:-1])}
    sizes = {}
    for a, n in allrows.items():
        nxt = nextstart.get(a)
        sizes[a] = min(n, nxt - a) if nxt else n

    callers = {}
    for i in range(len(text) - 5):
        if text[i] == 0xE8:
            t = tva + i + 5 + struct.unpack_from("<i", text, i + 1)[0]
            callers[t] = callers.get(t, 0) + 1

    code = Code(text, tva, CRT_LO)
    raw = {}
    for a, n in sizes.items():
        if a >= CRT_LO or n < 1 or a - tva < 0 or a - tva + n > len(text):
            continue
        # Evidence is gathered over the row's whole clamped range, so that
        # an un-enumerated neighbour's call sites still count against
        # their target; the row's own classification uses `end`.
        raw[a] = scan(code, text, tva, a, n)

    def _live_in(t):
        if t in raw:
            return raw[t][2]
        nxt = min((x for x in allstarts if x > t), default=t + 64)
        raw[t] = scan(code, text, tva, t, min(nxt - t, 4096))
        return raw[t][2]

    # A site is evidence only when the callee reads what it was handed.
    # 0x100219e5 has %edx live into 0x1007b8d0 -- the dead remainder of a
    # `div` -- and 0x1007b8d0 is `mov $imm,%eax ; ret`, which reads nothing.
    scans, entries = {}, {}
    for a, (end, sites, live_in) in list(raw.items()):
        if a not in sizes:
            continue
        kept = []
        for pos, t, live in sites:
            passed = live & _live_in(t)
            if passed:
                kept.append((pos, t, passed))
                entries.setdefault(t, set()).add(
                    f"{'/'.join(sorted(passed))} live at {pos:#x}")
        scans[a] = (end, kept, live_in)
        if live_in:
            entries.setdefault(a, set()).add(f"reads {'/'.join(sorted(live_in))} live-in")
    fronts = {t: front_of(text, tva, t, allstarts) for t in entries}
    return text, tva, sizes, callers, entries, fronts, scans, allstarts


def main():
    opts = dict(a.split("=", 1) if "=" in a else (a, True)
                for a in sys.argv[1:] if a.startswith("--"))
    top = int(opts.get("--top", 50))
    want = opts.get("--tier")

    text, tva, sizes, callers, entries, fronts, scans, allstarts = load()

    # everything our source already claims, by address in a symbol or comment
    claimed = set()
    for f in os.listdir("src/engine/d3d"):
        if not f.endswith((".cpp", ".h")):
            continue
        blob = open(os.path.join("src/engine/d3d", f),
                    encoding="utf-8", errors="replace").read()
        claimed.update(int(m, 16) for m in re.findall(r"0x(1[0-9a-f]{7})", blob))

    # Rows the enumeration gets wrong, confirmed by hand against the shipped
    # bytes (progress/d3d-enum-suspect.csv). An address that is really an
    # operand byte looks exactly like a function start, so each is recorded
    # once and dropped here rather than rediscovered.
    bogus = set()
    sus = "progress/d3d-enum-suspect.csv"
    if os.path.exists(sus):
        for r in csv.DictReader(open(sus)):
            if r.get("severity") == "bogus":
                bogus.add(int(r["addr"], 16))

    slots = slot_map(text, tva)
    by_addr = {v: k for k, v in slots.items()}

    # A register entry with a stack-ABI front is one function with it, and
    # the front is where the work is. `merged[front] = body`. Most fronts
    # are not enumerated at all (0x100265a0, 0x10028030, 0x1003b960 are
    # not); the ones that are come in as shim rows, and are replaced.
    merged = {f: t for t, f in fronts.items() if f is not None}

    rows = []
    for a in sorted(set(sizes) | set(merged)):
        if a >= CRT_LO or a in bogus or a in claimed:
            continue
        off = a - tva
        why, marks = [], []
        ncall = callers.get(a, 0)
        if a in merged:
            # Front + register entry = one function. 0x10039bd0 (12 bytes of
            # loads) + 0x10039bdc (the body) is 0x10039bd0's function; our
            # own build of sub_10079f30 opens with the same twelve bytes.
            # Which address reproduces is for the source to find out:
            # 0x1007dec4 did so from +4, its front's load being dead.
            t = merged[a]
            if t in claimed or t not in sizes:
                continue
            n = (t - a) + sizes[t]
            end, sites, live_in = scans.get(t, (t + sizes[t], [], set()))
            ncall += callers.get(t, 0)
            marks.append("two-entry")
            why.append(f"register entry at +{t - a} (0x{t:08x}), "
                       f"{callers.get(t, 0)} caller(s) enter there")
        else:
            n = sizes[a]
            if n < 1 or off < 0 or off + n > len(text):
                continue
            body = text[off:off + n]
            # NOT A FUNCTION. A row that is only the stack-ABI loads with no
            # register entry behind it that anything reaches -- the loads
            # were split off by the enumerator and the body row follows as
            # an ordinary function. Dropped, as before.
            if is_shim(body):
                continue
            # An entry that is not 4-byte aligned and is not reached by any
            # call is almost always an interior the enumerator split on a
            # jump target.
            if a % 4 and not callers.get(a):
                continue
            end, sites, live_in = scans.get(a, (a + n, [], set()))
        real = max(1, min(end, a + n) - a)
        body = text[off:off + real]

        marks += sorted({tag for pat, tag in FINGERPRINTS if pat in body})
        if any(c in X87 for c in body):
            marks.append("x87")
        # `mov <reg>,(%esp)` and `mov <reg>,disp8(%esp)`, any register, and
        # the `movl $imm,...(%esp)` form. modrm rm=100 with a 0x24 SIB is the
        # %esp addressing; mod 00 and 01 both count. The first cut matched
        # 89 04 24 only, and 0x10021c20 and 0x10021ce0 pass their argument in
        # %edx (89 14 24) -- same idiom, and they leaked straight into A.
        for i in range(len(body) - 2):
            if body[i] in (0x89, 0xC7) and (body[i + 1] & 0x07) == 0x04 \
                    and (body[i + 1] & 0xC0) in (0x00, 0x40) \
                    and body[i + 2] == 0x24:
                marks.append("mov-arg")
                break
        # An entry `sub $N,%esp` unwound by a MISMATCHED `add $M,%esp` before
        # the pushes, in a function with no locals: the unknown compiler
        # keeps %esp aligned across calls this way and cl does not.
        sub = add = None
        for i in range(len(body) - 2):
            if body[i] == 0x83 and body[i + 1] == 0xEC and sub is None:
                sub = body[i + 2]
            elif body[i] == 0x83 and body[i + 1] == 0xC4 and sub is not None \
                    and add is None:
                add = body[i + 2]
        if sub is not None and add is not None and sub != add:
            marks.append("esp-align")
        # A dead stack slot of ANY size, not just four bytes. 0x1003b690
        # opens eight and closes them in front of all six of its returns.
        for i in range(len(body) - 2):
            if body[i] == 0x83 and body[i + 1] == 0xC4 and body[i + 2] in (
                    0x04, 0x08, 0x0C, 0x10):
                marks.append("dummy-slot")
                break
        # Reads a register it never loaded: an argument arrived in it. Only
        # the first instruction is checked, which is where it shows.
        if body[:2] in (b"\x8b\x01", b"\x8b\x08", b"\x8b\x10") or \
           body[:1] == b"\xd9" or body[:2] == b"\x8b\xc1":
            marks.append("reg-arg")

        # REGISTER ENTRY (Z). Either half of the evidence will do: a call
        # site somewhere in the image hands it %eax/%edx, or its own entry
        # path reads them before anything wrote them. 0x1007b580 is both --
        # `mov $1,%eax ; call 0x1007b580` at 0x1007b8a0, and nothing in
        # front of it but a bare `ret` and padding. With a front, the row
        # is kept only as a signpost to the merged row at the front.
        is_entry = a in entries and a not in merged
        if is_entry:
            marks.append("reg-abi-body")
            f = fronts.get(a)
            if f is not None:
                why.append(f"register entry of 0x{f:08x} -- the function starts there")
            else:
                why.append("register-only ABI, no stack entry: "
                           + "; ".join(sorted(entries[a])))
        # REGISTER-ABI CALLER (Y). One of this row's own direct calls has
        # %eax or %edx live into it. The row says which, so the reader can
        # see the blocked expression before deciding anything.
        mine = [s for s in sites if a <= s[0] < a + real]
        if mine and not is_entry:
            marks.append("reg-call")
            shown = ", ".join(f"0x{t:08x}({'/'.join(sorted(l))})"
                              for _, t, l in mine[:3])
            why.append(f"{len(mine)} register-ABI call(s): {shown}"
                       + (" ..." if len(mine) > 3 else ""))
        marks = sorted(set(marks))

        slot = by_addr.get(a)
        if slot is not None:
            why.insert(0, f"slot +0x{slot:02x} {NAMES.get(slot, '')}".strip())
        if ncall:
            why.append(f"{ncall} caller(s)")
        # Leverage only. `marks` is kept and printed because it still tells a
        # reader what SHAPE to expect, but it no longer gates anything --
        # except the two register-ABI facts, which are not shape.
        if is_entry:
            tier = "Z"                      # not a function in its own right
        elif mine:
            tier = "Y"                      # one call expression is not C
        elif slot is not None:
            tier = "A"
        elif ncall >= 8:
            tier = "B"
        elif ncall >= 1:
            tier = "C"
        else:
            tier = "D"
        rows.append((tier, a, real, marks, why, ncall))

    # A first (verifiable today, smallest first); then named slots; then
    # by how many callers depend on the answer.
    order = {"A": 0, "B": 1, "C": 2, "D": 3, "Y": 4, "Z": 9}
    # Smallest first inside a tier. Callers still decide the tier, but a
    # 5840-byte body is not a good first row for a batch however many sites
    # reach it -- small functions verify fast and calibrate the source style
    # for the large ones.
    rows.sort(key=lambda t: (order[t[0]], t[2]))
    if want:
        rows = [r for r in rows if r[0] == want]

    print(f"{len(rows)} candidates outside the CRT and not already claimed\n")
    print(f"  {'tier':<4} {'addr':<12}{'size':>5}  {'blockers':<28} why")
    for tier, a, n, marks, why, _ in rows[:top]:
        print(f"  {tier:<4} {a:#010x} {n:>5}  "
              f"{(','.join(marks) or '— none, VERIFIABLE NOW'):<28} "
              f"{'; '.join(why)}")

    out = opts.get("--csv")
    if out:
        with open(out, "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["tier", "addr", "size", "blockers", "why"])
            for tier, a, n, marks, why, _ in rows[:top]:
                w.writerow([tier, f"0x{a:08x}", n, ",".join(marks), "; ".join(why)])
        print(f"\nwrote {out}")


if __name__ == "__main__":
    main()
