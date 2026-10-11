#!/usr/bin/env python3
"""Generate src/engine/d3d/d3ddata.cpp: d3d.ren's globals, defined once each.

The d3d TUs reach almost all of d3d.ren's .data/.bss through `extern`
declarations named after the shipped address (g_1008caf0, g_p1008b87c,
g_Mat1008ba1c ...) or by a descriptive name whose address is recorded in a
comment beside the extern (g_CV_Bilinear // 0x1008ca98) -- and define them
nowhere, so the whole-DLL link (`build/engine.sh d3d-link`) cannot resolve
them. This tool defines them, mechanically, from the shipped image.

 1. THE SET: every data symbol (`?name@@3...`) some d3d object references and
    no d3d object defines (d3ddata.obj itself excluded, so a re-run after a
    build regenerates the same file).

 2. ITS ADDRESS, in order of trust:
      a. the address in the name (its trailing 8 hex digits, inside d3d.ren's
         data sections);
      b. the `// 0x...` comment on the extern declaration (every declaration
         that carries one must agree);
      c. the value the relocation carries in the shipped image, read back
         out of a byte-identical function placed at its OWN address (the
         evidence tools/linkresolve.py uses, restricted to functions whose
         sub_<addr> name agrees with the placement -- two functions identical
         up to relocations would otherwise trade evidence).
    (c) is also checked against (a)/(b): a placed reference that reads back
    outside the object is reported -- a reference named for one global that
    really reaches another.

 3. ITS EXTENT: from its address to the next known address (the next symbol
    of the set, or the lowest address a symbol defined elsewhere is read back
    at), capped at the section end -- and no wider than the spelling needs
    when that is known: a builtin, a pointer, an `extern T x[N]`, or a
    struct/class whose sizeof the declaring TUs agree on
    (src/engine/d3d/link/d3dsizes.txt, written by --probe). A spelling that
    needs MORE than that gap is a view laid across several globals (a
    struct spelled over a run of separately-named scalars, a [3][256] table
    whose rows other TUs name one by one); it is not defined and is
    reported, since one object cannot also be three.

 4. ONE OBJECT PER ADDRESS: all the spellings of one address that fit (18
    pointer types for 0x1008b87c, a scalar and an array for 0x1008b880 ...)
    get ONE definition, the canonical spelling; each other spelling becomes
    a weak alias of it in src/engine/d3d/link/aliases.rsp (a delimited block
    this tool owns and rewrites). Nothing is defined twice. The canonical
    spelling is the one that can carry the extent and the initial bytes: an
    array (pointer elements first when the object holds pointers), else a
    struct/class, else a builtin/pointer.

 5. ITS TYPE: the definition produces the declared mangled name. A
    struct/class-typed definition goes through an OPAQUE stand-in of the
    same tag and class-key (`struct CVarR5C { unsigned char m_Bytes[N]; }`):
    the mangled name depends only on the tag, and only this TU sees the
    stand-in. A struct/class object spans the whole gap to the next known
    global -- the spelling may be a view (an interface, a base class, an
    empty stand-in) smaller than the object really there, and whatever
    reaches past the view must not land on a neighbour -- so N is the
    largest extent any object of that tag has (never below its sizeof; an
    array element keeps its exact sizeof). An object can then be larger
    than its shipped slot: that wastes bytes but aliases nothing.

 6. ITS INITIAL VALUE: objects in a section's raw part get the shipped
    bytes (zero ones get an explicit zero initialiser); a relocated word is
    emitted as an address constant:
      - of another object of this file (+ offset);
      - of a function, through `extern "C" d3dfn_<addr>` aliased to the
        function our objects define at that address (named sub_<addr>, in
        d3dcheck's NAMED table, or placed byte-identical there);
      - of a string in .data1, as that string literal.
    An object holding any other pointer is not defined, and is listed.
    Objects past the raw end are BSS: no initialiser, in address order.

 IIDs (IID_*, shipped in .rdata from the DirectX 7 SDK's dxguid.lib, which
 the toolchain does not have -- VC6's dxguid predates DX7) are defined here
 with the shipped GUID bytes like any other initialised global.

Usage:
  d3ddata.py [--write] [--report]
      default: print the plan summary; --write writes d3ddata.cpp and the
      alias block; --report lists every warning and skipped symbol.
  d3ddata.py --probe     (inside the build container: needs icl)
      measure sizeof of every tag a global is spelled with, as each
      declaring TU compiles it; writes src/engine/d3d/link/d3dsizes.txt.
"""
import bisect
import collections
import glob
import os
import re
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
from exedump import Pe  # noqa: E402
from exematch import find  # noqa: E402
from linkresolve import parse_obj  # noqa: E402

D3D = os.path.join(ROOT, "src/engine", "d3d")
OUT_CPP = os.path.join(D3D, "d3ddata.cpp")
ALIASES = os.path.join(D3D, "link", "aliases.rsp")
SIZES_TXT = os.path.join(D3D, "link", "d3dsizes.txt")
VIEWS = os.path.join(D3D, "link", "d3dviews.txt")
BEGIN = "# ---- BEGIN other spellings of one d3d.ren global (generated) ----"
END = "# ---- END other spellings of one d3d.ren global ----"
ADDR_RE = r"100(?:8[89a-f]|9[0-9a-f]|a[0-9a-d])[0-9a-f]{3}"

# (name, start, end of raw data, end of section)
SECS = [(".rdata", 0x10088000, 0x10088F5C, 0x10088F5C),
        (".data", 0x10089000, 0x1008B000, 0x100A4000),
        (".data1", 0x100A4000, 0x100AC060, 0x100AC060),
        ("_DATA", 0x100AD000, 0x100ADC20, 0x100ADC20)]

# Runtime-library functions a table points at (the linked CRT supplies them).
CRT = {
    # `push $0x19 (_RT_PUREVIRT); call 0x1008190b (_amsg_exit); pop %ecx; ret`:
    # libcmt's _purecall, in the pure-virtual slots of the interface vtables.
    0x10080905: "__purecall",
}

# icl /Qip stack entries a table points at, each onto the register entry it
# falls into (4 bytes on).  The stack entry is `mov 0x4(%esp),%eax` and the
# body overwrites %eax before reading it, so entering the body directly is
# the same function; the body is written in a TU without its shipped
# same-file caller, so no pair is emitted for it (guides/09, /Qip).
STACK_SHIMS = {
    # vtable 0x10089a20 +0x04; body 0x1007dd24 (d3dr5c.cpp) starts
    # `push %ebp ... mov 0x30(%ecx),%eax`.
    0x1007DD20: "?sub_1007dd24@Batch1007dd24@@QAEJPAUD3DDevice7R5C@@KK@Z",
    # vtable 0x10089a20 +0x0c; body 0x1007def4 (w10_1007a280_os.cpp) starts
    # `push %ebp ... mov %ecx,%ebx; mov 0x30(%ebx),%eax`.
    0x1007DEF0: "?sub_1007def4@Obj1007dcc4@@QAEXXZ",
    # vtable 0x10089a20 +0x14; body 0x1007dfa4 (d3dr5b.cpp) starts
    # `push %ebp ... mov %ecx,%ebx; mov 0x34(%ebx),%eax`.
    0x1007DFA0: "?sub_1007dfa4@CVertBufSet@@QAEPAEXZ",
}

# Addresses no name, comment or placed function supplies, with the evidence.
MANUAL = {
    # d3d_CreateWorld (0x10033160) tests NoLMPages then LightmapPages:
    # `mov 0x1008ca34,%eax` at 0x10033188, `mov 0x1008d978,%eax` at
    # 0x10033191, and clears the latter at 0x100331ba.
    "?g_bNoLMPages@@3IA": 0x1008CA34,
    "?g_bLightmapPages@@3IA": 0x1008D978,
    # d3dhigh.cpp's comment sits on the line above the extern; the table's
    # shipped bytes are 1,1,2,4,0,0,0,1 -- bytes per pixel by format.
    "?g_nFormatBytesPerPixel@@3PAKA": 0x10089890,
}

# Extents no spelling carries, with the evidence: an unbounded byte view
# (`extern uint8 g_x[]`, mangled `unsigned char *`) otherwise runs to the next
# known global and swallows whatever lies between.
EXTENT = {
    # d3dbatchb.cpp's 0x1007b964 stores `$0x100a9810` into its +0x18: sixteen
    # zero bytes in .data1, and the string "[FULLBRITE] - " starts at
    # 0x100a9820. The gap to the next known global also covers the switch
    # jump table at 0x100a99e8 (targets 0x1003d37d..0x1003d4ee, inside
    # 0x1003d350's body), which is not this object's.
    0x100A9810: 0x10,
    # w3_10016234_os.cpp's +0x63c array vtable: twelve .text pointers
    # (0x1001d240 .. 0x1001cf40), the shape of the 0x100892e0 and 0x10089310
    # vtables beside it. The words from 0x10089370 (0, 1, 800.0f, 0,
    # 0x100a4fe0) are another object no TU names.
    0x10089340: 0x30,
}

SIZES = {}          # tag -> sizeof (only sizes > 1: a size-1 tag is a view)


def load_sizes():
    if os.path.exists(SIZES_TXT):
        for line in open(SIZES_TXT):
            f = line.split()
            if f and not f[0].startswith("#") and f[1] != "?" and "DISAGREE" not in f:
                if int(f[1], 16) > 1:
                    SIZES[f[0]] = int(f[1], 16)


def section(a):
    for name, lo, rawend, end in SECS:
        if lo <= a < end:
            return name, a < rawend, end, rawend
    return None, False, None, None


# ------------------------------------------------------------------------ #
#  A demangler for DATA symbols only: ?name@@3<type><storage>
# ------------------------------------------------------------------------ #
PRIM = {"X": "void", "D": "char", "E": "unsigned char", "C": "signed char",
        "F": "short", "G": "unsigned short", "H": "int", "I": "unsigned int",
        "J": "long", "K": "unsigned long", "M": "float", "N": "double"}
PSIZE = {"D": 1, "E": 1, "C": 1, "F": 2, "G": 2, "H": 4, "I": 4, "J": 4,
         "K": 4, "M": 4, "N": 8}
CC = {"A": "__cdecl", "E": "__thiscall", "G": "__stdcall", "I": "__fastcall"}
KEY = {"U": "struct", "V": "class", "T": "union"}


class Unsupported(Exception):
    pass


class Ty:
    """kind: prim | tag | ptr | arr | fn.  decl(inner) -> C declarator."""

    def __init__(self, kind, **kw):
        self.kind, self.const, self.cptr = kind, False, False
        self.__dict__.update(kw)

    def decl(self, inner=""):
        k, cq = self.kind, (" const" if self.const else "")
        if k == "prim":
            return f"{self.name}{cq} {inner}".strip()
        if k == "tag":
            return f"{self.key} {self.name}{cq} {inner}".strip()
        if k == "ptr":
            c = " const" if self.cptr else ""
            if self.to.kind == "fn":
                f = self.to
                args = ", ".join(a.decl() for a in f.args) or "void"
                return f.ret.decl(f"({f.cc}*{c} {inner})({args})".replace(" )", ")"))
            p = f"*{c} {inner}".strip()
            if self.to.kind == "arr":
                p = f"({p})"
            return self.to.decl(p)
        if k == "arr":
            return self.of.decl(f"{inner}[{self.n}]")
        raise Unsupported(k)

    def tags(self):
        if self.kind == "tag":
            yield self.key, self.name
        for sub in ("to", "of", "ret"):
            if hasattr(self, sub):
                yield from getattr(self, sub).tags()
        for a in getattr(self, "args", ()):
            yield from a.tags()

    def size(self):
        k = self.kind
        if k == "prim":
            return PSIZE.get(self.code)
        if k == "ptr":
            return 4
        if k == "arr":
            s = self.of.size()
            return s * self.n if s else None
        if k == "tag":
            return SIZES.get(self.name)
        return None

    def depth(self):
        return 1 + self.of.depth() if self.kind == "arr" else 0


def _num(s, i):
    """MSVC encoded number -> (value, next index)."""
    if s[i].isdigit():
        return int(s[i]) + 1, i + 1
    v = 0
    while s[i] != "@":
        v = v * 16 + (ord(s[i]) - ord("A"))
        i += 1
    return v, i + 1


def parse_type(s, i):
    c = s[i]
    if c in PRIM:
        return Ty("prim", name=PRIM[c], code=c), i + 1
    if c in KEY:
        j = s.index("@@", i + 1)
        nm = s[i + 1:j]
        if "@" in nm or "?" in nm:
            raise Unsupported("nested/template tag " + nm)
        return Ty("tag", key=KEY[c], name=nm), j + 2
    if c in ("P", "Q"):
        cv = s[i + 1]
        if cv == "6":                           # pointer to function
            cc = CC.get(s[i + 2])
            if not cc:
                raise Unsupported("calling convention")
            ret, j = parse_type(s, i + 3)
            fargs, seen = [], []
            if s[j] == "X":
                j += 1
            else:
                while s[j] not in "@Z":
                    if s[j].isdigit():          # back-reference to an argument
                        fargs.append(seen[int(s[j])])
                        j += 1
                        continue
                    k0 = j
                    a, j = parse_type(s, j)
                    fargs.append(a)
                    if j - k0 > 1:              # only multi-char types are remembered
                        seen.append(a)
                j += 1
            if s[j] != "Z":
                raise Unsupported("function type end")
            return Ty("ptr", to=Ty("fn", ret=ret, args=fargs, cc=cc), cptr=(c == "Q")), j + 1
        if cv not in "AB":
            raise Unsupported("cv " + cv)
        if s[i + 2] == "Y":                     # pointer to array
            nd, j = _num(s, i + 3)
            dims = []
            for _ in range(nd):
                d, j = _num(s, j)
                dims.append(d)
            t, j = parse_type(s, j)
            t.const = t.const or cv == "B"
            for d in reversed(dims):
                t = Ty("arr", of=t, n=d)
            return Ty("ptr", to=t, cptr=(c == "Q")), j
        to, j = parse_type(s, i + 2)
        to.const = to.const or cv == "B"
        return Ty("ptr", to=to, cptr=(c == "Q")), j
    raise Unsupported("type code " + c)


def demangle_data(sym):
    """-> (name, Ty, object-is-const)."""
    m = re.match(r"\?([A-Za-z_]\w*)@@3(.*)$", sym)
    if not m:
        raise Unsupported("not a global datum")
    t, j = parse_type(m.group(2), 0)
    rest = m.group(2)[j:]
    if rest not in ("A", "B"):
        raise Unsupported("storage " + rest)
    return m.group(1), t, rest == "B"


# ------------------------------------------------------------------------ #
#  Inputs
# ------------------------------------------------------------------------ #
def relocs(pe):
    d = pe.d
    rva, sz = struct.unpack_from("<II", d, pe.pe + 24 + 96 + 5 * 8)
    o = pe.va2off(pe.base + rva)
    end, out = o + sz, set()
    while o < end:
        page, bs = struct.unpack_from("<II", d, o)
        if bs == 0:
            break
        for k in range((bs - 8) // 2):
            e = struct.unpack_from("<H", d, o + 8 + 2 * k)[0]
            if e >> 12 == 3:
                out.add(pe.base + page + (e & 0xFFF))
        o += bs
    return out


def objects():
    return sorted(p for p in glob.glob(os.path.join(D3D, "Release", "*.obj"))
                  if os.path.basename(p).lower() != "d3ddata.obj")


def gather():
    """-> pe, want{sym: refs}, defined, ev{sym: {addr}}, funcs{addr: {sym}}"""
    from d3dcheck import NAMED
    pe = Pe(os.path.join(ROOT, "out", "reference", "d3d.ren"))
    tva, tvs, trp = pe.secs[".text"]
    text = pe.d[trp:trp + tvs]
    parsed, defined, undef = {}, set(), collections.defaultdict(set)
    for o in objects():
        f, d, u = parse_obj(open(o, "rb").read())
        parsed[o] = f
        defined |= d
        for s in u:
            undef[s].add(os.path.basename(o))
    want = {s: r for s, r in undef.items()
            if s not in defined and re.match(r"\?\w+@@3", s)}

    placed, funcs = {}, collections.defaultdict(set)
    for o, fns in parsed.items():
        for n, (code, mask, _r) in fns.items():
            # icl's `X$$1` is X's body behind the stack-aligning entry X
            # (big48180 & co.): the address is X's, not a second claimant.
            # Likewise `X$`, the /Qip register entry behind X's stack entry
            # (tools/d3dcheck.py measures it at X + the entry's length).
            claim = None if "$$" in n or re.match(r"\?[^@]*\$@", n) \
                else _claim(n, NAMED)
            if claim:
                funcs[claim].add(n)
            if len(code) - len(mask) < 8:
                continue
            hits = list(find(text, tva, code, mask))
            if claim:
                if claim in hits:
                    placed[(o, n)] = claim
            elif len(hits) == 1:
                placed[(o, n)] = hits[0]
                funcs[hits[0]].add(n)
    for a, n in CRT.items():
        funcs[a].add(n)
    for a, n in STACK_SHIMS.items():
        if n in defined:
            funcs[a].add(n)
    # An address several of our functions claim is still one function when
    # some of the claimants are byte-identical there (placed at it): keep
    # those -- the rest are unplaced stand-ins -- and, identical bytes being
    # identical code, the first by name when more than one is (0x1001dbb0,
    # 0x1005a2f0: one CMoArray member written in two TUs; 0x10050770: W7's
    # GenBegin, identical, against d3dr4c.cpp's unplaced 14-byte spelling).
    at = collections.defaultdict(set)
    for (_o, n), base in placed.items():
        at[base].add(n)
    for a, names in funcs.items():
        if len(names) > 1:
            ok = sorted(names & at.get(a, set()))
            if ok:
                funcs[a] = {ok[0]}
    ev = collections.defaultdict(set)
    for (o, n), base in placed.items():
        for off, kind, s in parsed[o][n][2]:
            if kind == "abs":
                ev[s].add(struct.unpack_from("<I", text, base - tva + off)[0])
    return pe, want, defined, ev, funcs


def _claim(sym, named):
    """The address a function's name claims, exactly as tools/d3dcheck.py
    reads it: its full name in SYMBOLS, else an address in the leaf, else in
    the class, else NAMED."""
    from d3dcheck import SYMBOLS
    if sym in SYMBOLS:
        return SYMBOLS[sym]
    parts = re.sub(r"^\?", "", sym).split("@")
    leaf = parts[0]
    m = re.search(r"(1[0-9a-f]{7})", leaf)
    if not m and len(parts) > 1:
        m = re.search(r"(1[0-9a-f]{7})", parts[1])
    if m:
        a = int(m.group(1), 16)
        return a if 0x10001000 <= a < 0x10088000 else None
    return next((v for k, v in sorted(named.items(), key=lambda kv: -len(kv[0]))
                 if k in leaf), None)


def source_decls(names):
    """name -> [(file, line, [comment addresses], [dims])] of extern lines."""
    out = collections.defaultdict(list)
    for f in sorted(glob.glob(os.path.join(D3D, "*.cpp")) +
                    glob.glob(os.path.join(D3D, "*.h"))):
        if os.path.basename(f) == "d3ddata.cpp":
            continue
        for i, line in enumerate(open(f, errors="replace"), 1):
            if not re.match(r"\s*extern\b", line):
                continue
            code = line.split("//")[0]
            for m in re.finditer(r"\b([A-Za-z_]\w*)\s*((?:\[\s*\w*\s*\])*)", code):
                if m.group(1) in names:
                    c = re.findall(r"0x(" + ADDR_RE + r")\b", line, re.I)
                    dims = re.findall(r"\[\s*(\w*)\s*\]", m.group(2))
                    out[m.group(1)].append((os.path.basename(f), i,
                                            [int(x, 16) for x in c], dims))
    return out


# ------------------------------------------------------------------------ #
#  The plan
# ------------------------------------------------------------------------ #
class Sp:
    """One spelling: a mangled data symbol with its type and address."""

    def __init__(self, sym, name, ty, const, addr, how, ev, refs, arrdims):
        self.sym, self.name, self.ty, self.const = sym, name, ty, const
        self.addr, self.how, self.ev, self.refs = addr, how, ev, refs
        self.arrdims = arrdims          # dims of declarations matching this spelling
        self.alias_only = False         # never the canonical spelling (see plan)

    @property
    def is_array(self):
        """A pointer-typed spelling that is really an array."""
        t = self.ty
        return t.kind == "ptr" and t.to.kind != "fn" and \
            (bool(self.arrdims) or t.to.kind == "arr")

    @property
    def elem(self):
        return self.ty.to if self.is_array else None

    def need(self):
        """-> (bytes, exact): the size this spelling needs -- its type's size,
        an array's declared count (else at least one element), and never
        less than the furthest a placed function reaches through it. exact
        is False when the size is only a lower bound."""
        reach = max([v - self.addr + 1 for v in self.ev if v >= self.addr] + [0])
        if self.is_array:
            es = self.elem.size()
            ns = [int(d[0]) for d in self.arrdims if d and d[0].isdigit()]
            if es and ns:
                # A loop over the array that icl biases to its END (index
                # counting up from -size to 0) carries displacements up to
                # one element past the last one -- w18_10035e20_osoi.cpp's
                # `0x1008da44(%eax)` for g_1008d9b0[4] -- without touching
                # anything there. Within one element, the declared count
                # stands.
                if reach <= es * (max(ns) + 1):
                    return es * max(ns), True
                return max(es * max(ns), reach), True
            return max(es or 0, reach), False
        n = self.ty.size()
        return (max(n, reach), True) if n is not None else (reach, False)


def plan():
    pe, want, defined, ev, funcs = gather()
    byname = collections.defaultdict(list)
    for s in want:
        byname[re.match(r"\?(\w+)@@3", s).group(1)].append(s)
    decls = source_decls(set(byname))
    skipped, warn, sps = [], [], []
    stand_in = {}
    for s in list(want):
        # A table of pointers to MEMBER functions (`T (C::*g[N])(...)`,
        # mangled `?g@@3PAP8C@@...`): C cannot express that type without
        # the class, but each entry is one code pointer (icl, a class with no
        # virtual base: w7_10056c60_os.cpp's `this->*g_10089718[i]` scales the
        # index by 4, and the shipped words at 0x10089718 are four .text
        # addresses). The object is defined as `void *g[N]` -- a spelling of
        # our own -- and the referenced spelling becomes its alias.
        m = re.match(r"\?(\w+)@@3[PQ][AB]P8\w+@@", s)
        if m:
            syn = f"?{m.group(1)}@@3PAPAXA"
            stand_in[s] = syn
            want.setdefault(syn, set()).update(want[s])
    for s, refs in sorted(want.items()):
        try:
            nm, ty, cobj = demangle_data(stand_in.get(s, s))
        except Unsupported as e:
            skipped.append((s, f"type not covered by the demangler ({e})"))
            continue
        m = re.search(r"(" + ADDR_RE + r")$", nm)
        cs = {a for d in decls.get(nm, []) for a in d[2]}
        e = sorted(ev.get(s, ()))
        if m:
            a, how = int(m.group(1), 16), "name"
        elif s in MANUAL:
            a, how = MANUAL[s], "manual"
        elif len(cs) == 1:
            a, how = next(iter(cs)), "comment"
        elif len(cs) > 1:
            skipped.append((s, "declarations disagree: " +
                            ", ".join(hex(x) for x in sorted(cs))))
            continue
        elif e:
            a, how = e[0], "placed reference"
        else:
            skipped.append((s, "no address: no name, comment or placed reference"))
            continue
        if not section(a)[0]:
            skipped.append((s, f"{a:#x} is in no data section"))
            continue
        # declarations whose array depth matches this spelling
        depth = ty.to.depth() + 1 if ty.kind == "ptr" and ty.to.kind != "fn" else -1
        arrdims = [d[3] for d in decls.get(nm, []) if d[3] and len(d[3]) == depth]
        sps.append(Sp(s, nm, ty, cobj, a, how, e, sorted(refs), arrdims))
        sps[-1].alias_only = s in stand_in

    # every address something is known to start at
    bounds = {sp.addr for sp in sps}
    for s, addrs in ev.items():
        if s in defined and not s.startswith(("?sub_", "??_C")) and addrs:
            if section(min(addrs))[0]:
                bounds.add(min(addrs))
    known = sorted(bounds)

    def gap(a):
        end = section(a)[2]
        i = bisect.bisect_right(known, a)
        nxt = known[i] if i < len(known) else end
        return min(nxt, end) - a

    groups = collections.defaultdict(list)
    for sp in sps:
        groups[sp.addr].append(sp)

    # addresses a TU already DEFINES a data global at, under any name (placed
    # by the name's address, else by a byte-identical function's reference):
    # the spellings wanted there are aliased onto that definition, never given
    # a second one -- two objects at one shipped address link cleanly and are
    # two objects at run time (d3dneutral.cpp's g_1008bf68 vs g_pBackSurface).
    existing = {}
    for s in sorted(defined):
        m = re.match(r"\?(\w+)@@3", s)
        if not m or s.startswith("??_C"):
            continue
        am = re.search(r"(" + ADDR_RE + r")$", m.group(1))
        cand = {int(am.group(1), 16)} if am else set(ev.get(s, ()))
        if len(cand) == 1:
            a = next(iter(cand))
            if section(a)[0]:
                existing.setdefault(a, s)

    regions = _regions(groups, existing, known, gap, skipped)
    in_region = {a for r in regions for a in r["addrs"]}

    defs, aliases = [], []
    for a in sorted(groups):
        if a in in_region:
            continue
        if a in existing:
            for sp in groups[a]:
                aliases.append((sp.sym, existing[a], a))
            continue
        g = gap(a)
        fit = []
        for sp in groups[a]:
            n, _exact = sp.need()
            if n > g:
                skipped.append((sp.sym, f"{a:#x}: this spelling needs {n:#x} bytes but "
                                f"the next global starts {g:#x} on -- a view laid over "
                                f"several globals; re-spell it in {', '.join(sp.refs)}"))
            else:
                fit.append(sp)
        if not fit:
            continue
        # a struct/class object takes the whole gap: a view class (an
        # interface, a base) can be smaller than the object really there,
        # and a constructor reaching past it must not land on a neighbour
        # (a const object is read-only data: it is exactly its type)
        ext = max((sp.need()[0] if sp.need()[1] and (sp.ty.kind != "tag" or sp.const)
                   else max(sp.need()[0], g)) for sp in fit)
        defs.append(dict(addr=a, gap=g, ext=ext, spellings=fit))

    # opaque stand-in sizes: the largest object of the tag (an array
    # element keeps its true sizeof)
    elem_tags = {sp.elem.name for d in defs for sp in d["spellings"]
                 if sp.is_array and sp.elem.kind == "tag"}
    tag_ext = collections.defaultdict(list)
    for d in defs:
        for sp in d["spellings"]:
            if sp.ty.kind == "tag":
                tag_ext[sp.ty.name].append(d["ext"])
    opaque = {}
    for n, lst in tag_ext.items():
        opaque[n] = SIZES[n] if n in elem_tags and n in SIZES else \
            max(lst + [SIZES.get(n, 0)])

    def sz(t):
        if t.kind == "tag":
            return opaque.get(t.name) or t.size()
        return t.size()

    # an unsized array in initialised data: a table of pointers runs as far
    # as the relocated words do, a string as far as its terminator
    R = relocs(pe)
    for d in defs:
        a = d["addr"]
        if not section(a)[1] or any(sp.need()[1] for sp in d["spellings"]):
            continue
        arrs = [sp for sp in d["spellings"] if sp.is_array]
        if any(sp.elem.kind == "ptr" for sp in arrs):
            run = 0
            while a + 4 * run in R:
                run += 1
            if run:
                d["ext"] = min(d["ext"], 4 * run)
        elif any(sp.elem.kind == "prim" and sp.elem.code in "DE" for sp in arrs) \
                and pe.cstr(a, 1 << 16) is not None:
            d["ext"] = min(d["ext"], len(_cstring(pe, a)) + 1)

    for d in defs:
        if d["addr"] in EXTENT:
            d["ext"] = min(d["ext"], EXTENT[d["addr"]])

    # initial values, then the canonical spelling
    starts = {d["addr"]: d for d in defs}
    order = sorted(starts)
    out, keymap = [], {}
    for d in defs:
        a, ext = d["addr"], d["ext"]
        sec, raw, _end, rawend = section(a)
        d["sec"], d["raw"] = sec, raw
        d["init"], d["ptrs"] = None, {}
        bad, missing = None, []
        if raw:
            n = min(ext, rawend - a)
            d["init"] = pe.d[pe.va2off(a):pe.va2off(a) + n]
            for r in sorted(x for x in R if a <= x < a + n):
                if (r - a) % 4:
                    bad = f"unaligned pointer at +{r - a:#x}"
                    break
                v = struct.unpack_from("<I", d["init"], r - a)[0]
                i = bisect.bisect_right(order, v) - 1
                rg = _region_of(regions, v)
                if rg:
                    d["ptrs"][r - a] = ["view", rg["addr"], v - rg["addr"]]
                elif i >= 0 and v < order[i] + starts[order[i]]["ext"]:
                    d["ptrs"][r - a] = ["obj", order[i], v - order[i]]
                elif len(funcs.get(v, ())) == 1:
                    d["ptrs"][r - a] = ["fn", v, next(iter(funcs[v]))]
                elif funcs.get(v):
                    bad = f"+{r - a:#x} points to {v:#x}, which several of our functions claim"
                    break
                elif _existing_at(existing, gap, v):
                    e = _existing_at(existing, gap, v)
                    d["ptrs"][r - a] = ["ext", e, v - e, existing[e]]
                elif section(v)[0] == ".data1" and pe.cstr(v, 4096) is not None:
                    d["ptrs"][r - a] = ["str", v, _cstring(pe, v)]
                elif 0x10001000 <= v < 0x10088000:
                    # keep going: report EVERY unwritten target, not the first
                    missing.append(f"+{r - a:#x}->{v:#x}")
                else:
                    bad = f"+{r - a:#x} points to {v:#x} (nothing of ours)"
                    break
        if missing and not bad:
            bad = ("points to functions we have not written: "
                   + ", ".join(missing))
        if bad:
            for sp in d["spellings"]:
                skipped.append((sp.sym, f"{a:#x}: {bad}"))
            continue
        canon = None
        for cand in _choose(d, sz):
            # one TU gives a tag one class-key: a spelling that needs the
            # other key cannot be defined here (another spelling may be)
            if any(keymap.get(n, k) != k for k, n in cand.ty.tags()):
                continue
            d["canon"] = cand
            try:
                _definition(d, sz)
            except Unsupported:
                continue
            canon = cand
            break
        if canon is None and d["ptrs"] and ext % 4 == 0:
            # A table of pointers every TU spells as bytes (`extern uint8
            # g_10089530[]`, a vtable a constructor only stores): a byte
            # array cannot carry the relocations, so the object is defined as
            # `void *g[N]` -- a spelling of our own, as for the member-pointer
            # tables above -- and the byte spellings become its aliases.
            s0 = min(d["spellings"], key=lambda sp: sp.sym)
            syn = f"?{s0.name}@@3PAPAXA"
            if all(sp.sym != syn for sp in d["spellings"]):
                nm, ty, cobj = demangle_data(syn)
                cand = Sp(syn, nm, ty, cobj, a, s0.how, [], [], [[str(ext // 4)]])
                d["canon"] = cand
                try:
                    _definition(d, sz)
                    d["spellings"].append(cand)
                    canon = cand
                except Unsupported:
                    pass
        if canon is None:
            for sp in d["spellings"]:
                skipped.append((sp.sym, f"{a:#x}: no spelling can carry {ext:#x} bytes"
                                + (" with pointers" if d["ptrs"] else "")))
            continue
        d["canon"] = canon
        for k, n in canon.ty.tags():
            keymap[n] = k
        out.append(d)
    # a pointer to an object this file does not define after all
    changed = True
    while changed:
        changed = False
        live = {d["addr"]: d for d in out}
        for d in list(out):
            for o, p in d["ptrs"].items():
                if p[0] == "obj" and p[1] not in live:
                    for sp in d["spellings"]:
                        skipped.append((sp.sym, f"{d['addr']:#x}: +{o:#x} points into "
                                        f"{p[1]:#x}, which is not defined"))
                    out.remove(d)
                    changed = True
                    break
    live = {d["addr"]: d for d in out}
    # a tag's true size: its sizeof, else (a view) the commonest spacing
    gaps = collections.defaultdict(list)
    for d in out:
        if d["canon"].ty.kind == "tag":
            gaps[d["canon"].ty.name].append(d["gap"])
    typical = {n: SIZES.get(n) or collections.Counter(g).most_common(1)[0][0]
               for n, g in gaps.items()}
    for d in out:
        for p in d["ptrs"].values():
            if p[0] == "obj":
                p[1] = live[p[1]]["canon"].name
        a, ext = d["addr"], d["ext"]
        c = d["canon"]
        full = typical.get(c.ty.name) if c.ty.kind == "tag" else None
        if full and full > d["gap"]:
            warn.append(f"{c.name} ({a:#x}) is a {c.ty.key} {c.ty.name}, {full:#x} bytes "
                        f"elsewhere, but the next global starts {d['gap']:#x} on: that global "
                        f"is probably one of its fields, named separately")
        if c.is_array and not any(sp.need()[1] for sp in d["spellings"] if sp.is_array) \
                and ext == sz(c.elem) and not d["raw"]:
            warn.append(f"{c.name} ({a:#x}) is an unsized array with room for ONE element "
                        f"before the next global: that global is probably its element [1]")
        for sp in d["spellings"]:
            if sp is not d["canon"]:
                aliases.append((sp.sym, d["canon"].sym, a))
            for v in sp.ev:
                if not a <= v < a + max(ext, 1):
                    warn.append(f"{sp.name} ({a:#x}) is read back as {v:#x} by a placed "
                                f"function in {', '.join(sp.refs)}: it names one global and "
                                f"reaches another")
    _region_init(pe, regions, R, live, funcs, skipped, existing, gap)
    regions[:] = [rg for rg in regions if rg.get("ok", True)]
    return out, aliases, skipped, warn, opaque, sz, regions


def _regions(groups, existing, known, gap, skipped):
    """Globals laid out by address in COFF sections (tools/mkviewobj.py)
    instead of defined one by one in C++ here.

    ALL of the zero-initialised globals (BSS): icl lays a TU's .bss out in
    REVERSE definition order, so C++ definitions in address order come out
    back to front -- and the d3d TUs reach neighbours through each other
    everywhere: a view block copied whole (big26728.cpp copies the 0x4f8-byte
    `XForm26728 g_1008b880` that thirty other TUs name field by field), a
    table indexed past the element another TU names separately
    (g_a1008e664[stage] against g_1008e668/66c/670), a pointer to the whole
    block passed down (`uint8 g_1008b880[]`). One section per run of BSS,
    every spelling a name at its shipped offset, lays each run out exactly as
    shipped. A run ends where a TU defines a global itself (that object is
    the TU's; the run stops short of it) or at the section end.

    In initialised data, only where a spelling needs more than the gap to
    the next global (a view laid over several globals): that object becomes
    a region, carrying the shipped bytes and relocations (_region_init)."""
    out = []
    ex = sorted(a for a in existing if section(a)[0])

    def limit(a):
        i = bisect.bisect_right(ex, a)
        sec_end = section(a)[2]
        return min(ex[i], sec_end) if i < len(ex) and section(ex[i])[0] == section(a)[0] \
            else sec_end

    def region(members, end, raw):
        keep = []
        for b in members:
            for sp in groups[b]:
                if b + sp.need()[0] > end:
                    skipped.append((sp.sym, f"{b:#x}: needs {sp.need()[0]:#x} bytes, reaching "
                                    f"past {end:#x}, where a TU defines a global of its own"))
                else:
                    keep.append((b - members[0], sp.sym))
        if keep:
            out.append(dict(addr=members[0], end=end, addrs=list(members),
                            syms=sorted(keep), raw=raw))

    # BSS: every run between TU-defined globals
    run = []
    for a in sorted(x for x in groups if section(x)[0] and not section(x)[1] and x not in existing):
        if run and limit(run[0]) != limit(a):
            region(run, limit(run[0]), False)
            run = []
        run.append(a)
    if run:
        region(run, limit(run[0]), False)

    # initialised data: view conflicts only
    addrs = sorted(a for a in groups if section(a)[1])
    done = set()
    for a in addrs:
        if a in done or a in existing or \
                not any(sp.need()[0] > gap(a) for sp in groups[a]):
            continue
        end = a + max(sp.need()[0] for sp in groups[a])
        members = [a]
        changed = True
        while changed:
            changed = False
            for b in addrs:
                if a < b < end and b not in members:
                    members.append(b)
                    end = max([end] + [b + sp.need()[0] for sp in groups[b]])
                    changed = True
        members.sort()
        done.update(members)
        i = bisect.bisect_left(known, end)
        end = min(known[i] if i < len(known) else section(a)[2], limit(a), section(a)[3])
        if any(a < x < end for x in ex) or members[-1] >= end:
            continue          # reported by the ordinary path as before
        region(members, end, True)
    return out


def _region_of(regions, v):
    for rg in regions:
        if rg["addr"] <= v < rg["end"]:
            return rg
    return None


def _existing_at(existing, gap, v):
    """The address of the object a TU defines (not this file) that v points
    into: a static initialiser's object (w57_1003b1c0.cpp's g_1008e6d0) that
    a table in initialised data holds the address of. None if there is none."""
    cands = [e for e in existing if e <= v]
    if not cands:
        return None
    e = max(cands)
    return e if v < e + max(gap(e), 1) else None


def _region_init(pe, regions, R, live, funcs, skipped, existing=None, gap=None):
    """Initial bytes and relocations of a region in a section's raw part."""
    for rg in regions:
        a, end = rg["addr"], rg["end"]
        _sec, raw, _e, rawend = section(a)
        rg["bytes"], rg["relocs"], rg["fns"] = None, [], []
        if not raw:
            continue
        n = min(end, rawend) - a
        b = bytearray(pe.d[pe.va2off(a):pe.va2off(a) + n])
        bad, missing = None, []
        for r in sorted(x for x in R if a <= x < a + n):
            v = struct.unpack_from("<I", b, r - a)[0]
            other = _region_of(regions, v)
            starts = sorted(live)
            i = bisect.bisect_right(starts, v) - 1
            if other:
                sym, off = f"_d3dview_{other['addr']:08x}", v - other["addr"]
            elif i >= 0 and v < starts[i] + live[starts[i]]["ext"]:
                sym, off = live[starts[i]]["canon"].sym, v - starts[i]
            elif len(funcs.get(v, ())) == 1:
                sym, off = f"_d3dfn_{v:08x}", 0
                rg["fns"].append((v, next(iter(funcs[v]))))
            elif existing and _existing_at(existing, gap, v):
                e = _existing_at(existing, gap, v)
                sym, off = existing[e], v - e
            elif not funcs.get(v) and 0x10001000 <= v < 0x10088000:
                # keep going: report EVERY unwritten target, not the first
                missing.append(f"+{r - a:#x}->{v:#x}")
                continue
            else:
                what = "several of our functions claim it" if funcs.get(v) else \
                    "nothing a region can point at"
                bad = f"+{r - a:#x} points to {v:#x} ({what})"
                break
            struct.pack_into("<I", b, r - a, off)
            rg["relocs"].append((r - a, sym))
        if missing and not bad:
            bad = ("points to functions we have not written: "
                   + ", ".join(missing))
        if bad:
            rg["ok"] = False
            for _o, s in rg["syms"]:
                skipped.append((s, f"{a:#x}: view region {a:#x}..{end:#x}: {bad}"))
            continue
        rg["bytes"] = bytes(b)


def write_views(regions):
    L = ["# src/engine/d3d/link/d3dviews.txt -- generated.",
         "# d3d.ren objects that the d3d TUs reach under several names at several offsets",
         "# (a view block and its separately-named fields). tools/mkviewobj.py turns this into",
         "# link/d3dviews.obj at d3d-link time: one COFF section per region, one name per offset."]
    for rg in regions:
        a, size = rg["addr"], rg["end"] - rg["addr"]
        al = 16
        while a % al:
            al //= 2
        L.append(f"region {a:#x} {size:#x} {'data' if rg['bytes'] is not None else 'bss'} {al}")
        L.append(f"sym 0x0 _d3dview_{a:08x}")
        for off, s in rg["syms"]:
            L.append(f"sym {off:#x} {s}")
        if rg["bytes"] is not None:
            b = rg["bytes"]
            for o in range(0, len(b), 32):
                if any(b[o:o + 32]):
                    L.append(f"bytes {o:#x} {b[o:o + 32].hex()}")
            for o, s in rg["relocs"]:
                L.append(f"reloc {o:#x} {s}")
    open(VIEWS, "w").write("\n".join(L) + "\n")


def _cstring(pe, v):
    off = pe.va2off(v)
    return pe.d[off:pe.d.find(b"\0", off)]


def _choose(d, sz):
    """Candidate canonical spellings, best first (see the docstring)."""
    ext, fit, has_ptr = d["ext"], d["spellings"], bool(d["ptrs"])

    def key(sp):
        return sp.name, sp.sym
    arrays = []
    fit = [sp for sp in fit if not sp.alias_only]
    for sp in sorted(fit, key=key):
        if sp.is_array and not sp.ty.cptr:
            es = sz(sp.elem)
            if es and ext % es == 0 and (not has_ptr or _ptr_ok(sp.elem)):
                arrays.append(sp)
    if has_ptr:
        arrays.sort(key=lambda sp: (sp.elem.kind != "ptr",) + key(sp))
    out = list(arrays)
    if has_ptr:
        out += [sp for sp in sorted(fit, key=key) if sp.ty.kind == "ptr" and not sp.is_array
                and ext == 4 and list(d["ptrs"]) == [0]]
        return out
    out += [sp for sp in sorted(fit, key=key) if sp.ty.kind == "tag" and (sz(sp.ty) or 0) >= ext]
    out += [sp for sp in sorted(fit, key=key) if not sp.is_array and sp.ty.size() == ext]
    return out


def _ptr_ok(elem):
    """Can an array of `elem` hold pointers?"""
    return elem.kind == "ptr" or (elem.kind == "prim" and elem.code in "HIJK") \
        or (elem.kind == "arr" and _ptr_ok(elem.of))


# ------------------------------------------------------------------------ #
#  Emission
# ------------------------------------------------------------------------ #
def emit(defs, opaque, sz):
    tags, full = {}, set()
    for d in defs:
        c = d["canon"]
        for k, n in c.ty.tags():
            tags.setdefault(n, k)
        t = c.elem if c.is_array else c.ty
        if t.kind == "tag":
            full.add(t.name)
    fns = sorted({(p[1], p[2]) for d in defs for p in d["ptrs"].values() if p[0] == "fn"})
    L = []
    w = L.append
    w("// *********************************************************************** //")
    w("//  d3ddata.cpp -- d3d.ren's globals, ONE definition per shipped address.")
    w("//")
    w("//  Generated from the retail d3d.ren and the d3d objects.")
    w("//")
    w("//  The d3d TUs declare these `extern`; this file defines each once, with")
    w("//  the shipped initial bytes, in address order within each section. The")
    w("//  other spellings of an address (another type, another name) are weak")
    w("//  aliases of the definition here -- src/engine/d3d/link/aliases.rsp,")
    w("//  the generated d3d.ren-globals block. Each trailing comment gives the address, the")
    w("//  extent and where the address came from (the name, the extern's")
    w("//  comment, or a placed function's relocation).")
    w("//")
    w("//  A struct/class-typed global is defined through an OPAQUE stand-in of")
    w("//  its tag: the mangled name depends only on the tag and its class-key,")
    w("//  and nothing but this TU sees the stand-in's layout. An object spans")
    w("//  the whole gap to the next known global (the spelling may be a view --")
    w("//  an interface or base -- smaller than the object really there), so a")
    w("//  stand-in is as large as the largest gap any global of its tag has,")
    w("//  and never smaller than the sizeof the TUs agree on (link/d3dsizes.txt).")
    w("//")
    w("//  d3dfn_<addr> is the function our objects define at that address; the")
    w("//  alias block maps each onto its real name.")
    w("// *********************************************************************** //")
    w("")
    fwd = sorted(n for n in tags if n not in full)
    for n in fwd:
        w(f"{tags[n]} {n};")
    if fwd:
        w("")
    for n in sorted(full):
        size = opaque.get(n) or SIZES.get(n)
        w(f"{tags[n]} {n} {{ public: unsigned char m_Bytes[{size:#x}]; }};")
    if full:
        w("")
    for a, _n in fns:
        w(f'extern "C" void d3dfn_{a:08x}();')
    if fns:
        w("")
    exts = sorted({p[1] for d in defs for p in d["ptrs"].values() if p[0] == "ext"})
    for a in exts:
        w(f'extern "C" unsigned char d3dobj_{a:08x}[];   // defined by a TU (alias block)')
    if exts:
        w("")
    views = sorted({p[1] for d in defs for p in d["ptrs"].values() if p[0] == "view"})
    for a in views:
        w(f'extern "C" unsigned char d3dview_{a:08x}[];   // link/d3dviews.txt')
    if views:
        w("")
    for sec in [s[0] for s in SECS]:
        ds = [d for d in defs if d["sec"] == sec]
        for title, group in (("initialised", [d for d in ds if d["raw"]]),
                             ("zero-initialised (BSS)", [d for d in ds if not d["raw"]])):
            if not group:
                continue
            w("")
            w("// ======================================================================= //")
            w(f"//  {sec}, {title}")
            w("// ======================================================================= //")
            for d in group:
                w(_definition(d, sz))
    w("")
    return "\n".join(L)


def _definition(d, sz):
    c, ext = d["canon"], d["ext"]
    how = "/".join(sorted({sp.how for sp in d["spellings"]}))
    more = len(d["spellings"]) - 1
    cmt = f"// {d['addr']:#x}  {ext:#x}  ({how}" + \
        (f"; +{more} alias{'es' if more > 1 else ''}" if more else "") + ")"
    kw = "extern " if c.const else ""
    init = d["init"]
    if c.is_array:
        es = sz(c.elem)
        decl = c.elem.decl(f"{c.name}[{ext // es}]")
        if init is None:
            return f"{decl};   {cmt}"
        if c.elem.kind == "prim" and c.elem.code in "DE" and len(init) == ext \
                and init.endswith(b"\0") and b"\0" not in init[:-1]:
            lit = _ptr_expr(("str", 0, init[:-1]), None)
            return f"{kw}{decl} = {lit};   {cmt}"
        return f"{kw}{decl} = {_array_init(c.elem, ext // es, es, init, d['ptrs'], sz)};   {cmt}"
    decl = _objconst(c.ty).decl(c.name) if c.const else c.ty.decl(c.name)
    if c.ty.kind == "tag":
        if init is None:
            return f"{kw}{decl};   {cmt}"
        b = init.rstrip(b"\0")
        return f"{kw}{decl} = {{ {{ {', '.join(f'{x:#04x}' for x in b) or '0'} }} }};   {cmt}"
    if init is None:
        return f"{kw}{decl}{' = 0' if c.const else ''};   {cmt}"
    return f"{kw}{decl} = {_scalar(c.ty, init, 0, d['ptrs'])};   {cmt}"


def _objconst(t):
    """The same type with the OBJECT const (storage `B`): `T* const`, `T const`."""
    import copy
    t = copy.copy(t)
    if t.kind == "ptr":
        t.cptr = True
    else:
        t.const = True
    return t


def _ptr_expr(p, ty):
    """An address constant for a relocated word (ty None: the bare literal)."""
    if p[0] == "obj":
        _k, name, off = p
        e = f"&{name}" if not off else f"(unsigned char*)&{name} + {off:#x}"
    elif p[0] == "fn":
        e = f"&d3dfn_{p[1]:08x}"
    elif p[0] == "view":
        e = f"d3dview_{p[1]:08x} + {p[2]:#x}" if p[2] else f"d3dview_{p[1]:08x}"
    elif p[0] == "ext":
        e = f"d3dobj_{p[1]:08x} + {p[2]:#x}" if p[2] else f"d3dobj_{p[1]:08x}"
    else:
        s = p[2].decode("latin-1")
        e = '"' + "".join(ch if 32 <= ord(ch) < 127 and ch not in '"\\' else
                          f"\\{ord(ch):03o}" for ch in s) + '"'
        if ty is None:
            return e
    return f"({ty.decl()})({e})"


def _scalar(t, b, off, ptrs):
    if off in ptrs:
        return _ptr_expr(ptrs[off], t)
    if t.kind == "prim":
        if t.code == "M":
            return _float(b[off:off + 4])
        if t.code == "N":
            return repr(struct.unpack_from("<d", b, off)[0])
        v = int.from_bytes(b[off:off + PSIZE[t.code]], "little")
        return f"{v:#x}" if v else "0"
    if t.kind == "ptr":
        if struct.unpack_from("<I", b, off)[0]:
            raise Unsupported("non-null unrelocated pointer")
        return "0"
    if t.kind == "tag":
        chunk = b[off:off + SIZES.get(t.name, 0)].rstrip(b"\0")
        return "{ { " + (", ".join(f"{x:#04x}" for x in chunk) or "0") + " } }"
    raise Unsupported("initialiser of " + t.kind)


def _float(raw):
    f = struct.unpack("<f", raw)[0]
    s = repr(f)
    for p in range(6, 18):
        s = f"{f:.{p}g}"
        if struct.pack("<f", float(s)) == raw:
            break
    if not any(ch in s for ch in ".en"):
        s += ".0"
    return s + "f"


def _array_init(et, n, es, b, ptrs, sz):
    vals = []
    for i in range(n):
        if i * es >= len(b):
            vals.append("0")
        elif et.kind == "arr":
            sub = [_scalar(et.of, b, i * es + j * sz(et.of), ptrs) for j in range(et.n)]
            while sub and sub[-1] in ("0", "0.0f"):
                sub.pop()
            vals.append("{ " + (", ".join(sub) or "0") + " }")
        else:
            vals.append(_scalar(et, b, i * es, ptrs))
    while vals and vals[-1] in ("0", "0.0f", "{ 0 }", "{ { 0 } }"):
        vals.pop()
    return "{ " + (", ".join(vals) or "0") + " }"


def write_aliases(aliases, defs, regions=()):
    lines = open(ALIASES).read().split("\n")
    if BEGIN in lines:
        i, j = lines.index(BEGIN), lines.index(END)
        lines[i:j + 1] = []
    while lines and lines[-1] == "":
        lines.pop()
    block = [BEGIN,
             "# Each line maps a spelling of a d3d.ren global onto the ONE",
             "# definition src/engine/d3d/d3ddata.cpp gives that address (the comment above",
             "# it), or a d3dfn_<addr> function reference onto the function defined there."]
    for s, c, a in sorted(aliases, key=lambda x: (x[2], x[0])):
        block.append(f"# {a:#x}")
        block.append(f"/ALTERNATENAME:{s}={c}")
    fns = sorted({(p[1], p[2]) for d in defs for p in d["ptrs"].values() if p[0] == "fn"} |
                 {f for rg in regions for f in rg["fns"]})
    for a, n in fns:
        block.append(f"# {a:#x}")
        block.append(f"/ALTERNATENAME:_d3dfn_{a:08x}={n}")
    exts = sorted({(p[1], p[3]) for d in defs for p in d["ptrs"].values() if p[0] == "ext"})
    for a, n in exts:
        block.append(f"# {a:#x}  defined by a TU; reached from initialised data")
        block.append(f"/ALTERNATENAME:_d3dobj_{a:08x}={n}")
    block.append(END)
    open(ALIASES, "w").write("\n".join(lines + [""] + block) + "\n")


# ------------------------------------------------------------------------ #
#  --probe: sizeof every struct/class a global is spelled with, as the TUs
#  that declare it compile it. Each referencing TU is compiled once more
#  (icl, into Release/probe/) with an appended
#      extern "C" int dl1_sizes[] = { sizeof(T), ... };
#  and the values are read back out of the object.
# ------------------------------------------------------------------------ #
def _coff_int_array(path, name):
    d = open(path, "rb").read()
    _m, _nsec, _t, symoff, nsyms, optsz, _c = struct.unpack_from("<HHIIIHH", d, 0)
    strtab = d[symoff + nsyms * 18:]
    i = 0
    while i < nsyms:
        e = symoff + i * 18
        raw = d[e:e + 8]
        if raw[:4] == b"\0\0\0\0":
            o = struct.unpack_from("<I", raw, 4)[0]
            nm = strtab[o:strtab.find(b"\0", o)].decode("latin-1")
        else:
            nm = raw.rstrip(b"\0").decode("latin-1")
        value, secnum = struct.unpack_from("<Ih", d, e + 8)
        naux = d[e + 17]
        if nm == name and secnum > 0:
            sh = 20 + optsz + (secnum - 1) * 40
            rawsz, rawp = struct.unpack_from("<II", d, sh + 16)
            return [struct.unpack_from("<i", d, rawp + value + 4 * k)[0]
                    for k in range((rawsz - value) // 4)]
        i += 1 + naux
    return None


def probe():
    import subprocess
    _pe, want, _defined, _ev, _f = gather()
    need = collections.defaultdict(set)          # stem -> {(key, tag)}
    for s, refs in want.items():
        try:
            _n, ty, _c = demangle_data(s)
        except Unsupported:
            continue
        t = ty.to if ty.kind == "ptr" else ty
        while t.kind == "arr":
            t = t.of
        if t.kind != "tag":
            continue
        for r in refs:
            need[r[:-4]].add((t.key, t.name))
    pdir = os.path.join(D3D, "Release", "probe")
    os.makedirs(pdir, exist_ok=True)
    found = collections.defaultdict(dict)        # tag -> {stem: size}
    for stem in sorted(need):
        if not os.path.exists(os.path.join(D3D, stem + ".cpp")):
            continue
        pending = sorted(need[stem])
        # one compile per TU; where a tag is incomplete, one per tag
        for batch in [pending] + ([[t] for t in pending] if len(pending) > 1 else []):
            src = os.path.join(pdir, stem + "_sz.cpp")
            with open(src, "w") as f:
                f.write(f'#include "../../{stem}.cpp"\n')
                f.write('extern "C" int dl1_sizes[] = { ' +
                        ", ".join(f"sizeof({k} {n})" for k, n in batch) + ", 0 };\n")
            obj = src[:-4] + ".obj"
            if os.path.exists(obj):
                os.remove(obj)
            subprocess.run(["bash", "-c",
                            "export CL_REV=8168 WINEDEBUG=-all; source build/env.sh; "
                            "cd src/engine/d3d/Release/probe && run_icl /nologo /c /w "
                            "/D WIN32 /D NDEBUG /D _WINDOWS /D NO_PRAGMA_LIBS "
                            "/I '..\\..\\..\\..\\LT2\\sdk\\inc' /I '..\\..' "
                            f"{stem}_sz.cpp >/dev/null 2>&1"], cwd=ROOT)
            v = _coff_int_array(obj, "_dl1_sizes") if os.path.exists(obj) else None
            if v:
                for (_k, n), size in zip(batch, v):
                    found[n][stem] = size
                if batch is pending:
                    break
        print(stem, {n: found[n].get(stem) for _k, n in need[stem]}, flush=True)
    with open(SIZES_TXT, "w") as f:
        f.write("# sizeof(tag) as the d3d TUs compile it -- written by tools/d3ddata.py --probe.\n"
                "# tag  size  TUs.  A size of 0/1 is an empty view class (layout not modelled);\n"
                "# a tag the TUs size differently is marked DISAGREE and not used.\n")
        tags = {n for st in need.values() for _k, n in st}
        for n in sorted(tags):
            by = found.get(n, {})
            sizes = sorted(set(by.values()))
            if not sizes:
                f.write(f"{n} ? incomplete-or-unprobed\n")
            elif len(sizes) == 1:
                f.write(f"{n} {sizes[0]:#x} {' '.join(sorted(by))}\n")
            else:
                f.write(f"{n} {max(sizes):#x} DISAGREE " +
                        " ".join(f"{st}={size:#x}" for st, size in sorted(by.items())) + "\n")


def main():
    opts = set(sys.argv[1:])
    if "--probe" in opts:
        probe()
        return
    load_sizes()
    defs, aliases, skipped, warn, opaque, sz, regions = plan()
    print(f"defined {len(defs)} globals, {len(aliases)} other spellings aliased, "
          f"{len(regions)} view regions ({sum(len(r['syms']) for r in regions)} names), "
          f"{len(skipped)} symbols skipped, {len(warn)} warnings")
    if "--report" in opts:
        for rg in regions:
            print(f"  VIEW  {rg['addr']:#x}..{rg['end']:#x} ({rg['end'] - rg['addr']:#x} bytes, "
                  f"{'data' if rg['bytes'] is not None else 'bss'}): "
                  + ", ".join(f"+{o:#x} {s}" for o, s in rg["syms"]))
        for w in warn:
            print("  WARN ", w)
        for s, r in skipped:
            print("  SKIP ", s, "--", r)
    if "--write" in opts:
        open(OUT_CPP, "w").write(emit(defs, opaque, sz))
        write_aliases(aliases, defs, regions)
        write_views(regions)
        print("wrote", OUT_CPP, VIEWS, "and the alias block in", ALIASES)


if __name__ == "__main__":
    main()
