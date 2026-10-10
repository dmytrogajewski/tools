#!/usr/bin/env python3
"""The scoreboard: how much of every targeted binary we rebuild exactly.

For each shipped target this enumerates its functions (name + byte size)
and classifies our reconstruction of each as:

  identical  our bytes match the shipped function exactly (reloc-masked)
  near       we have the function but bytes differ (compiler-revision variance)
  small      we have the function but it is almost entirely relocations, so
             there is nothing for the oracle to anchor on (global-initializer
             COMDATs); reconstructed, not verified
  attempted  no match and no pairing, but there is evidence the address has
             been worked on (cited in src/, or recorded in progress/*.csv)
  todo       the shipped function has no counterpart in our sources yet
  unplaced   (ours, not theirs) a function of ours whose bytes occur nowhere
             in the image: not counted in either direction
  crt        outside the image's app region (the static CRT/MFC): listed,
             not counted

Ground truth is always the SHIPPED binary, so the denominator can't drift.
The engine images (lithtech.exe, server.dll, d3d.ren, NOLF.exe) come off the
disc into out/reference/ (`tools/discextract.py reference`), and so do the
Game of the Year game DLLs (CShell.dll, Object.lto, CRes.dll, out of
NOLFGOTY.REZ); CRes.dll holds no code of Monolith's, so it is scored
resource by resource instead (rows_for_resources); the seven
lithshared libraries are compared per COFF symbol against Monolith's prebuilt
.libs from the NOLF Source Code v1.003 release, found through $NOLF_SOURCE
and checked against progress/shipped-libs.sha256. Without NOLF_SOURCE the
libraries are left out and only the images are scored.

What is compared is what the build left in the tree: src/engine/*/Release/,
src/game/*/Final_Release/ (CRes.dll: src/game/ClientRes/Release/) and
src/lithshared/out/. Run `make` first.

Outputs (under out/progress/):
  functions.csv   one row per shipped function: target, name, size, status
  summary.json    per-target and total counts + bytes

tools/ratchet.py compares those with the recorded baseline in progress/.
`make progress` runs both.

Usage:
  progress.py [--nears[=target]] [--todo|--attempted|--near|--small[=target]]

--nears prints the near-match worklist instead of the summary: one line per
paired function with the shipped address, both sizes and the byte delta,
closest first. That order is the order to work them in -- a function that is
already size-exact usually needs one source change, where one off by a
hundred bytes needs a different shape. --todo (and the other statuses) list
that status's rows, largest first.
"""
import csv
import glob
import json
import os
import re
import struct
import sys
import tempfile

NEAR_PAIRS = []
# (label, shipped address) -> "module.obj:symbol", for every function exematch
# PLACES. rows_for_image deliberately reports placed functions as `sub_<addr>`
# because the scoreboard only needs the count, so the symbol is dropped there;
# a tool labelling a disassembly needs it. Written alongside `matched` and
# read by nothing in this file, exactly as NEAR_PAIRS above.
PLACED_SYMS = {}
# The same for the "small" pass (functions too short to anchor, placed by
# shape): (label, shipped address) -> "module.obj:symbol". Weaker evidence
# than PLACED_SYMS, so kept apart.
SMALL_SYMS = {}

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from objdiff import parse_obj  # noqa: E402
from libextract import extract  # noqa: E402
from d3dcheck import strip_pad, NAMED as D3D_NAMED  # noqa: E402
from exematch import parse_obj_masked, find  # noqa: E402
from exedump import Pe  # noqa: E402
from enumfuncs import Image as EnumImage, enumerate_functions  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# The retail images every denominator and oracle comes from, extracted from
# the disc images by `tools/discextract.py reference` (`make progress` does
# it); the build itself never reads them.
REFERENCE = "out/reference"

# Where this writes, and where the recorded baseline and the hand-curated
# evidence the enumeration reads live.
OUT = "out/progress"
DATA = "progress"

# lithshared libraries: (label, ours.lib); theirs is SHIPPED_LIB in the
# source release
LIBS = [
    ("StdLith",         "src/lithshared/out/StdLith.lib"),
    ("CryptMgr",        "src/lithshared/out/CryptMgr.lib"),
    ("RegMgr",          "src/lithshared/out/RegMgr.lib"),
    ("MFCStub",         "src/lithshared/out/MFCStub.lib"),
    ("ButeMgrNoMFC",    "src/lithshared/out/ButeMgrNoMFC.lib"),
    ("ltguimgr_LT2",    "src/lithshared/out/ltguimgr_LT2.lib"),
    ("LithFontMgr_LT2", "src/lithshared/out/LithFontMgr_LT2.lib"),
]
SHIPPED_LIB = "LT2/lithshared/libs/Release/{}.lib"
SHIPPED_SUMS = DATA + "/shipped-libs.sha256"


def shipped_lib_dir():
    """The directory holding the source release's prebuilt .libs, or None.

    $NOLF_SOURCE is the extracted NOLF Source Code v1.003 release (the
    directory holding LT2/); a directory holding the .libs themselves is
    accepted too. Every library is checked against SHIPPED_SUMS: a library
    from another release would score against the wrong ground truth, so a
    mismatch is fatal rather than a skip.
    """
    root = os.environ.get("NOLF_SOURCE", "")
    if not root:
        return None
    d = os.path.join(root, os.path.dirname(SHIPPED_LIB))
    if not os.path.isdir(d):
        d = root
    want = {}
    for line in open(os.path.join(REPO, SHIPPED_SUMS)):
        if line.strip():
            h, name = line.split()
            want[name] = h
    import hashlib
    bad = []
    for name, h in sorted(want.items()):
        p = os.path.join(d, name)
        if not os.path.isfile(p):
            bad.append("%s: missing" % name)
        elif hashlib.sha256(open(p, "rb").read()).hexdigest() != h:
            bad.append("%s: SHA-256 mismatch" % name)
    if bad:
        sys.exit("progress.py: NOLF_SOURCE=%s does not hold the NOLF Source "
                 "Code v1.003 libraries (%s):\n  %s" % (
                     root, SHIPPED_SUMS, "\n  ".join(bad)))
    return d

# engine binaries not yet started: tracked by .text byte scope (their functions
# have no symbols, and d3d/server use little inter-function padding so a carved
# count is only approximate — bytes are the exact, honest scope measure).
ENGINE = [
    ("lithtech.exe", REFERENCE + "/lithtech.exe"),
    ("d3d.ren",      REFERENCE + "/d3d.ren"),
    ("server.dll",   REFERENCE + "/server.dll"),
]

# The game DLLs, built from Monolith's source release (src/game/, Final
# Release: build/build.sh all final) and scored against the Game of the Year
# copies in NOLFGOTY.REZ: (label, shipped image, our objects' directory, the
# lithshared libraries it links, which are ours too -- as StdLith.lib in
# server.dll).
GAME = [
    ("CShell.dll", REFERENCE + "/CShell.dll", "src/game/ClientShellDLL/Final_Release",
     ["StdLith", "CryptMgr", "ltguimgr_LT2", "MFCStub", "ButeMgrNoMFC",
      "GameSpyClientMgr", "RegMgr", "LithFontMgr_LT2"]),
    ("Object.lto", REFERENCE + "/Object.lto", "src/game/ObjectDLL/Final_Release",
     ["StdLith", "CryptMgr", "MFCStub", "ButeMgrNoMFC", "GameSpyMgr", "RegMgr"]),
]
# CRes.dll: string tables, dialogs, bitmaps and text, and nothing but the
# CRT's DLL entry code -- so its resources are what is scored.
GAME_RES = ("CRes.dll", REFERENCE + "/CRes.dll", "src/game/ClientRes/Release/CRes.dll")
GAME_LABELS = {g[0] for g in GAME} | {GAME_RES[0]}

# Monolith's own code within each image. Everything above these ranges is the
# statically linked CRT (and MFC in NOLF.exe's case) — real code in the binary,
# but supplied by the toolchain, so it is NOT a reconstruction target. Counting
# it would understate progress as badly as counting only what we attempted
# would overstate it. Functions outside the range are recorded as "crt" and
# excluded from the totals.
#   NOLF.exe    : app region ends where the static CRT/MFC begins.
#   server.dll  : app region ends where libcmt.lib begins (_ftol); one
#                 Monolith island sits above it (APP_ISLANDS).
APP_REGION = {
    # NOLF.exe: Monolith's own code runs 0x401000 .. 0x409ed2 (the last app
    # function is CDibPal::InitRes at 0x409e70).  From 0x409ee0 on it is the
    # statically linked MFC (AfxGetMainWnd, the multi-monitor stubs, CArchive
    # operators) and then the CRT (_splitpath, strncpy, ...).
    "NOLF.exe":   (0x401000, 0x409ed2),
    # server.dll: the static CRT (libcmt.lib) begins at _ftol, 0x1003e0f0.
    # Evidence: every libcmt.lib member was searched for in .text (masked by
    # its own relocations, asm labels included); the lowest real hit is
    # __ftol at 0x1003e0f0, followed densely by _free, _malloc, _strcmp,
    # _memset, _strlen, ... Nothing between the old cut (0x10037b47, "last
    # function referencing a Class::Method error tag") and 0x1003e0f0 is
    # CRT; it is, in link order:
    #   ..0x1003a54f  engine TUs (unaligned /O1 code): CMoArray templates
    #                 byte-identical from worldbsp/collection.obj, the
    #                 BaseNew/BaseDelete pair at 0x10038329/0x10038366,
    #                 wt_VisibilityTraversal at 0x10038e77
    #   0x1003a550..  rezmgr.lib (16-aligned): rezmgr/rezmgr2/rezdir/
    #                 rezhash/rezfile/rezemu objects match 45+ functions
    #   0x1003d170..  StdLith.lib: struct_bank, l_allocator, stringholder,
    #                 dynarray, helpers, goodlinklist all match exactly
    # See also APP_ISLANDS for the one Monolith block linked after the CRT.
    "server.dll": (0x10001000, 0x1003e0f0),
    # lithtech.exe and d3d.ren are placed by a rigorous oracle rather than by
    # eye: the static CRT is the same library in every image built with this
    # toolchain, so it is byte-identical across them, and server.dll's
    # non-app region is already established. Taking each server.dll function
    # above 0x10037b47 (the server cut as it then stood -- since corrected to
    # 0x1003e0f0, so part of that sample was really rezmgr.lib/StdLith.lib,
    # which are also prebuilt and identical across images; the cut below is
    # still _ftol, as in server.dll) that is at least 32 bytes long and searching the other
    # image's .text for those exact bytes finds 71 of them in lithtech.exe and
    # 38 in d3d.ren — and in both cases they cluster into a dense tail.
    #
    # lithtech.exe's tail begins at _ftol (0x4928c0), with strncpy, _stricmp,
    # _findfirst, _vsnprintf and the CRT entry point following it. Nothing
    # Monolith wrote is identified above it: the highest error-tag literal
    # names a function at 0x48b820 and the highest function any of our objects
    # matches is a collection template at 0x4915c0. There are a few CRT hits
    # below the cut, interleaved with that template — the boundary is left
    # under them deliberately, because counting a little of the CRT against us
    # errs in the honest direction.
    #
    # d3d.ren's tail begins with the generic thunk run at 0x1007faa0, above a
    # stretch of int3 padding. Those thunks are the twenty-three functions the
    # engine objects "match" in d3d.ren by coincidence, which the project has
    # always refused to count; the boundary now says so in the denominator too.
    "lithtech.exe": (0x401000, 0x4928bf),
    "d3d.ren":      (0x10001000, 0x1007fa91),
    # The game DLLs link like server.dll: Monolith's objects, then the
    # lithshared libraries (StdLith, CryptMgr, ltguimgr_LT2, MFCStub,
    # ButeMgrNoMFC, the GameSpy library, RegMgr), then the static CRT, which
    # begins with libcimt.lib (the iostream classes) and goes on into
    # libcmt.lib. Found by taking every function of 24+ bytes in our own
    # linked image from the libraries and the CRT (our .map says which),
    # masking our relocations, and searching the retail .text for it: each
    # group's unique hits form one run, in our link order. libcimt's first
    # function is the same 43-byte function in both images (ifstream's,
    # just below ifstream::close, the group's first unique hit):
    #   CShell.dll: libcimt 0x100edab0 (close at 0x100edbd3), libcmt from
    #     0x100efd1b (??_M, our first libcmt function) to _findenv
    #     0x100fd769; above the libraries, ButeMgrNoMFC ends at
    #     ??3zSLink 0x100eb600 and GameSpyClientMgr is at 0x100eb6d0.
    #   Object.lto: libcimt 0x10141db0 (close at 0x10141ed3), libcmt from
    #     __cfltcvt_init 0x1014412f to __decomp 0x1015030e; RegMgr, the
    #     last library, ends at 0x10141d70 + 50 bytes.
    # The GameSpy library stays in the region: the retail one is the
    # GameSpy SDK, which src/lithshared/gamespy/ stands in for and will not
    # match, so those functions are honestly "todo".
    "CShell.dll":   (0x10001000, 0x100edab0),
    "Object.lto":   (0x10001000, 0x10141db0),
}


# Monolith code that the linker placed ABOVE the start of the static CRT,
# because its object was pulled from a library only after libcmt members had
# been. Scored like the app region; everything else above APP_REGION is CRT.
#   server.dll 0x1004bb20..0x1004bdcf: Lith.lib (src/lithshared/lith, built
#     by build/libs.sh lith): basehash.obj 0x1004bb20..0x1004bcfa, then
#     baselist.obj (CBaseList::InsertFirst/Delete 0x1004bd00/0x1004bd30) and
#     virtlist.obj (CVirtBaseList::InsertFirst/Delete 0x1004bd60/0x1004bd90),
#     all 13 byte-identical. The basehash iterator/bin code is what rezmgr
#     calls (CRezDir::GetNextSubDir 0x1003aca0 calls 0x1004bb20). /GX code
#     using __ehvec_ctor with rezmgr's bin dtor 0x1003b460 and the engine's
#     0x1000d25d/0x10028ace allocator pair; its twin sits after lithtech.exe's
#     CRT too (0x4a03d0..), byte-identical, so it is a prebuilt Monolith lib.
#     It is bracketed by CRT: an IAT jump thunk at 0x1004bb10 below it and
#     _strupr, __ehvec_ctor/dtor, __ArrayUnwind from 0x1004bdd0 on.
#   Deliberately NOT included: 0x1004c070..end, the C++ EH unwind funclets
#   and __ehhandler stubs (.text$x) of Monolith functions -- compiler-
#   generated, 2-5 unmasked bytes each, not separately reconstructable.
#   lithtech.exe 0x004a03d0..0x004a061f: the same library's basehash.obj
#     (CBaseHashItem::Next 0x4a03d0 .. CBaseHash::GetFirstInBin 0x4a0590)
#     and virtlist.obj (CVirtBaseList::InsertFirst 0x4a05b0, ::Delete
#     0x4a05e0), byte-identical to server.dll 0x1004bb20..0x1004bdc3 and
#     matched by src/lithshared/out/Lith.lib. Bracketed by CRT the same way:
#     an IAT jump thunk at 0x4a03be below it, __ehvec_ctor at 0x4a0620
#     above. (baselist.obj was pulled earlier here, into the app region at
#     0x0048de30..0x0048df0f.)
#   CShell.dll 0x100fd930..0x1010086f: LithFontMgr_LT2.lib, linked after
#     the CRT exactly as in our CShell.dll (our map: LithFontMgr_LT2 between
#     LIBCMT and __strupr). Its 29 unique hits run 0x100fda40 (LithFont's
#     constructor) .. 0x10100760 (LithBitmapFont::ClipFont, 250 bytes);
#     below it an import thunk (jmp [0x101080dc] at 0x100fd920, then int3
#     padding), above it, after a 4-byte nop-padded accessor at 0x10100860,
#     libcmt's __strupr at 0x10100870 (the same bytes as ours).
#   Object.lto 0x101503e0..0x101503ff: MFCStub's CRect::CRect(l, t, r, b)
#     (32 bytes, ret 0x10), linked after the CRT as in our Object.lto: an
#     import thunk at 0x101503d0 below it, CRT code from 0x10150400.
APP_ISLANDS = {
    "server.dll": [(0x1004bb20, 0x1004bdd0)],
    "lithtech.exe": [(0x004a03d0, 0x004a0620)],
    "CShell.dll": [(0x100fd930, 0x10100870)],
    "Object.lto": [(0x101503e0, 0x10150400)],
}


def in_app(label, a, lo, hi):
    """True when address a is Monolith code (APP_REGION or an island)."""
    if lo <= a < hi:
        return True
    return any(l <= a < h for l, h in APP_ISLANDS.get(label, ()))


# Where a prebuilt library linked into an image actually sits, when that has
# been established from evidence. A match for one of the library's functions
# outside its range is refused outright -- the library is linked as one
# block, so a hit elsewhere is the engine's own copy of the same bytes (a
# template instance, a 16-byte free wrapper) or a coincidence, never the
# library -- and its unanchorable functions claim "small" addresses by shape
# only inside it. The anchored-span rule in rows_for_image is the fallback
# for libraries not listed here; it is not enough on its own when one stray
# anchor stretches the span over engine code.
#   lithtech.exe StdLith.lib 0x490b50..0x4919df: every StdLith function with
#     a unique hit lands in one run, in link order struct_bank (sb_Init
#     0x490b50 .. sb_AllocateNewStructPage 0x490c20, ends 0x490c7e), helpers
#     ($E2/$E1 initializer thunks 0x490c80/0x490c90 -- `jmp` / `mov ecx,
#     &g_Helpers; jmp ??0CHelpers` -- then CHelpers::CHelpers 0x490ca0 ..
#     ExtractNames 0x490e50), stringholder (CStringHolder ctor 0x490f60 ..
#     ClearStrings 0x491270, its CMoArray<SBank> instances 0x4912d0..0x491739),
#     l_allocator ($E thunks 0x491740..0x49177b constructing the default
#     LAlloc global 0x4b4e88 with vtable 0x4a3c54, ~LAlloc 0x491780,
#     LAlloc::Alloc/Free 0x491790/0x4917a0 .. LAllocSimpleBlock::Clear
#     0x491940), dynarray (MoArray_FindElementMemcmp 0x491950) and
#     goodlinklist (GLinkedList_FindElementMemcmp 0x491990, ends 0x4919d1).
#     The same objects in the same order as server.dll 0x1003d170..0x1003e090,
#     byte-identical bar relocations (sb_Term2 0x490bd0 = 0x1003d1f0). Below
#     it, 0x490b40 is a 4-byte engine accessor after an engine function's
#     `ret`; above it, 0x4919e0 is a thiscall string copy matching no member
#     of either our or the shipped StdLith.lib (server.dll has the CRT's
#     fpinit straight after goodlinklist). Without the range, DefStdlithFree's
#     16 bytes credit the engine's own free wrapper at 0x436510, StdLith's
#     $E thunks take the engine's startup/atexit thunks 0x436880/0x4368b0,
#     and CAbstractIO::Open's 8 bytes (`mov eax,1; ret 8`) claim the tail of
#     0x4919e0 as a function at 0x491a10.
LIB_RANGE = {
    "lithtech.exe": {"StdLith.lib": (0x00490b50, 0x004919e0)},
}


def load_lib(path):
    """Return {symbol: masked_code} for a .lib (via objdiff.parse_obj)."""
    out = {}
    with tempfile.TemporaryDirectory() as td:
        extract(path, td)
        for m in sorted(glob.glob(td + "/*")):
            out.update(parse_obj(open(m, "rb").read()))
    return out


def rows_for_lib(label, ours_path, libdir):
    theirs = load_lib(os.path.join(libdir, label + ".lib"))
    ours = load_lib(os.path.join(REPO, ours_path)) if os.path.exists(
        os.path.join(REPO, ours_path)) else {}
    rows = []
    for name, code in theirs.items():
        if name not in ours:
            status = "todo"
        elif ours[name] == code:
            status = "identical"
        else:
            status = "near"
        rows.append((label, name, len(code), status))
    return rows


def _enum_with_sizes(path, label=None):
    img = EnumImage(path)
    starts, _src = enumerate_functions(img)
    # Proven starts the enumerator misses (progress/extent-splits.csv): a row
    # that is really two functions -- typically a 5/10-byte thunk and the
    # body it jumps into, which nothing else references -- can never pair.
    extra = set(_extent_splits().get(label, ())) | _REMAINDER_STARTS.get(label, set())
    if extra:
        starts = sorted(set(starts) | set(extra))
    # Rows PROVEN not to be code come out of the enumeration entirely -- see
    # _phantom_index. Sizes are computed from the ORIGINAL start list so that
    # dropping a phantom does not silently grow its neighbour's extent --
    # UNLESS the phantom's own exclude reason names the start just before it
    # as its container ("interior block of X (0x...)"). Then the bytes are
    # proven to be that function's, and leaving them off keeps a correct
    # reconstruction from ever pairing.
    phantom = _phantom_index(label)
    reasons = _phantom_reasons(label)
    sizes = {}
    for i, a in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else img.thi
        if a not in phantom:
            j = i + 1
            while j < len(starts) and starts[j] in phantom and \
                    _names_container(reasons.get(starts[j], ""), a):
                j += 1
                end = starts[j] if j < len(starts) else img.thi
        lo, hi = a - img.tlo, min(end - img.tlo, len(img.text))
        while hi > lo and img.text[hi - 1] in (0x90, 0xCC):
            hi -= 1
        sizes[a] = hi - lo
    if phantom:
        starts = [a for a in starts if a not in phantom]
    return img, starts, sizes


def _names_container(reason, a):
    """True if an exclude reason cites address `a` (any zero padding)."""
    for m in re.findall(r"0x0*([0-9a-fA-F]{5,8})\b", reason):
        if int(m, 16) == a:
            return True
    return False


_SPLITS = None


def _extent_splits():
    """progress/extent-splits.csv: image,addr,reason -- proven starts."""
    global _SPLITS
    if _SPLITS is not None:
        return _SPLITS
    out = {}
    try:
        for r in csv.DictReader(open(DATA + "/extent-splits.csv")):
            a = (r.get("addr") or "").strip().lower()
            if a.startswith("0x"):
                out.setdefault(r["image"].strip(), set()).add(int(a, 16))
    except OSError:
        pass
    _SPLITS = out
    return out


# progress/<prefix>-screen-exclude.csv records rows of ONE image, named by
# the file's prefix.
# They used to be read by ADDRESS ALONE, from every file, for every image --
# harmless while the only two files were lithtech.exe's (0x004xxxxx) and
# server.dll's, but d3d.ren shares server.dll's 0x10000000 base, so a d3d
# row would have dropped or relabelled whatever server.dll function sits at
# the same address. A file whose prefix is in this table applies only to its
# image; any other file (none today) keeps the old apply-everywhere reading.
_EXCLUDE_IMAGE = {
    "lithtech": "lithtech.exe",
    "server":   "server.dll",
    "d3d":      "d3d.ren",
}


def _exclude_files(label):
    """The screen-exclude files whose rows apply to image `label`."""
    if label in GAME_LABELS:
        return []          # every one of them is about an engine image
    out = []
    for f in sorted(glob.glob(DATA + "/*-screen-exclude.csv")):
        prefix = os.path.basename(f)[:-len("-screen-exclude.csv")]
        img = _EXCLUDE_IMAGE.get(prefix)
        if img is None or label is None or img == label:
            out.append(f)
    return out


_REASONS = {}


def _phantom_reasons(label=None):
    if label in _REASONS:
        return _REASONS[label]
    out = {}
    for f in _exclude_files(label):
        try:
            for r in csv.DictReader(open(f)):
                a = (r.get("addr") or "").strip().lower()
                if a.startswith("0x"):
                    try:
                        out[int(a, 16)] = (r.get("reason") or "")
                    except ValueError:
                        pass
        except OSError:
            pass
    _REASONS[label] = out
    return out


LINKED = {   # our whole-exe link, used as a second, unmasked oracle
    "NOLF.exe": "src/engine/launcher/Release/NOLF.exe",
}


# Starts found BEHIND an identical match that is shorter than its row: see
# rows_for_image. Consumed by _enum_with_sizes alongside extent-splits.csv.
_REMAINDER_STARTS = {}


def rows_for_image(label, exe_path, objs):
    """Score one image, then once more with the remainders of short matches.

    A row's size is reported as the length of what matched there, so when a
    SHORT function of ours matches at a start whose enumerated extent also
    holds more code -- icl's 14-byte stack-realign stub in front of a large
    body (0x1004b720: 14 of 11,204 bytes), or a register-entry stub whose
    caller's body follows (0x10024024: 108 of 5,672) -- the rest of the
    extent dropped out of funcs and bytes_total entirely: 28 KB of d3d.ren
    had silently left the denominator that way, and the body behind a stub
    had no row to pair with even when we had written it. The code behind
    such a match is its own function (the match ended in a ret or a tail
    jump), so its first non-padding byte becomes a start, and the image is
    scored again with those starts added.
    """
    rows = _rows_for_image(label, exe_path, objs)
    extra = _remainders(label, exe_path, rows)
    if extra:
        _REMAINDER_STARTS[label] = extra
        rows = _rows_for_image(label, exe_path, objs)
    return rows


def _remainders(label, exe_path, rows):
    p = os.path.join(REPO, exe_path)
    img, starts, sizes = _enum_with_sizes(p, label)
    have = set(starts)
    text, tlo = img.text, img.tlo
    out = set()
    for r in rows:
        if r[3] != "identical" or not r[1].startswith("sub_"):
            continue
        a = int(r[1][4:], 16)
        n, ext = r[2], sizes.get(a)
        if not ext or ext <= n:
            continue
        pos, end = a + n, a + ext
        moved = True
        while moved and pos < end:
            moved = False
            for pad in PADS:
                o = pos - tlo
                if text[o:o + len(pad)] == pad:
                    pos += len(pad)
                    moved = True
                    break
        # below 16 bytes it is the tail of an alignment run, not a function
        if end - pos >= 16 and pos not in have:
            out.add(pos)
    return out


# Trailing alignment encodings (tools/d3dcheck.py's PADS): VC6 pads with
# 0x90/0xCC, icl with multi-byte lea / mov nops.
PADS = [
    bytes.fromhex("8dbc2700000000"),
    bytes.fromhex("8db42600000000"),
    bytes.fromhex("8db600000000"),
    bytes.fromhex("8dbf00000000"),     # lea 0x0(%edi),%edi (6-byte form)
    bytes.fromhex("8d742600"),
    bytes.fromhex("8d7600"),
    bytes.fromhex("8bf6"),
    bytes.fromhex("8bff"),
    b"\x90",
    b"\xcc",
]


def _own_addr(label, n, funcs=None):
    """The shipped address a symbol of ours names: `sub_<addr>` (the d3d
    convention), or for d3d.ren an engine name tools/d3dcheck.py's NAMED
    table maps (d3d_IsInOptimized2D -> 0x10034c60). None if it names none.
    Only ever a preference: a placement there still needs the bytes to
    agree.

    d3d.ren, given our functions: icl's /Qip register entry `X$` (and the
    `X$$1` body behind a 14-byte stack-realigning entry) sits the stack
    entry X's length above the address X names, exactly as d3dcheck.py
    places it.  Without that, `sub_100124a0$` preferred 0x100124a0 -- the
    4-byte stack entry, already matched -- and an 8 KB body whose first 38
    bytes are the shipped 0x100124a4's lost the ranking to unrelated
    functions of similar length agreeing on 9% of their bytes."""
    m = re.search(r"sub_(1[0-9a-f]{7})", n)
    a = int(m.group(1), 16) if m else None
    if a is None and label == "d3d.ren":
        leaf = re.sub(r"^\?", "", n.split(":", 1)[-1]).split("@")[0]
        for k, v in sorted(D3D_NAMED.items(), key=lambda kv: -len(kv[0])):
            if k in leaf:
                a = v
                break
    if a is not None and label == "d3d.ren" and funcs is not None:
        mod, sym = n.split(":", 1) if ":" in n else ("", n)
        leaf = re.sub(r"^\?", "", sym).split("@")[0]
        for tail in ("$$1", "$"):
            if leaf.endswith(tail):
                base = "%s:%s" % (mod, sym.replace(leaf, leaf[:-len(tail)], 1))
                if base in funcs:
                    code = funcs[base][0]
                    a += len(strip_pad(bytes(code)) if tail == "$$1" else code)
                break
    return a


def _rows_for_image(label, exe_path, objs):
    """A PE image under reconstruction (exe or dll, no symbols).

    Denominator = every enumerated function inside the image's app region
    (see APP_REGION); functions outside it are the static CRT/MFC and are
    emitted as status "crt" so they stay visible but do not count.
    identical = an exematch hit for one of our objects.
    """
    p = os.path.join(REPO, exe_path)
    pe = Pe(p)
    tva, tvs, trp = pe.secs[".text"]
    text = pe.d[trp : trp + tvs]
    img, starts, sizes = _enum_with_sizes(p, label)

    # Key by module as well as symbol: compiler-generated names are numbered
    # per object, so `_$E192` exists in more than one of ours and a bare-symbol
    # dict silently drops all but the last — which loses whole initializer
    # groups from the count.
    funcs = {}
    unpadded = {}      # d3d symbols with icl lea-nop padding: the bare body
    from_lib = set()   # symbols pulled in from an already-scored .lib
    for o in objs:
        q = os.path.join(REPO, o)
        mod = os.path.basename(o)
        if o.lower().endswith(".lib"):
            with tempfile.TemporaryDirectory() as td:
                extract(q, td)
                for m in sorted(glob.glob(td + "/*")):
                    for k, v in parse_obj_masked(open(m, "rb").read()).items():
                        key = "%s:%s" % (mod, k)
                        # a .lib is many objects under ONE module name, and
                        # compiler-generated names repeat across them:
                        # StdLith's helpers.obj and l_allocator.obj both
                        # have `_$E1`/`_$E2`, and the later member silently
                        # replaced the earlier, so helpers' g_Helpers
                        # initializer (server 0x1003dd80, lithtech
                        # 0x490c90) could never claim its address. Name a
                        # repeat by its member too.
                        if key in funcs:
                            key = "%s:%s@%s" % (mod, k, os.path.basename(m))
                        funcs[key] = v
                        from_lib.add(key)
        else:
            for k, v in parse_obj_masked(open(q, "rb").read()).items():
                # instantiation drivers exist only to make VC6 emit template
                # COMDATs (packet.cpp); they are ours, not Monolith's, and
                # must neither score nor pair
                if "ForceInstantiation" in k:
                    continue
                # ??_H / ??_I are the vector constructor and destructor
                # iterators VC6 emits per object as a by-product of `new T[n]`
                # and `delete [] p`. With exceptions off it expands the loop
                # inline and never references them, so the linker drops them —
                # they are the compiler's code either way, not reconstructed
                # source, and scoring them counts a phantom against us.
                if k.startswith("??_H") or k.startswith("??_I"):
                    continue
                # icl (d3d.ren) pads a COMDAT with multi-byte lea-nops, not
                # 0x90/0xCC, and parse_obj_masked only strips the latter; the
                # leftover pad then has to match whatever the shipped image has
                # after the function, so an identical function could never be
                # found and scored "near". Strip the same set d3dcheck.py does.
                if "src/engine/d3d/" in o.replace("\\", "/"):
                    code, mask = v
                    body = strip_pad(code)
                    if body != code:
                        unpadded["%s:%s" % (mod, k)] = (
                            body, {i for i in mask if i < len(body)})
                funcs["%s:%s" % (mod, k)] = v

    # Several of our functions compile to identical code (CCard::GetName and
    # CPreview::GetName; DoDetailDialog and DoCustomizeDialog; the three
    # CDib::Init overloads), and the shipped linker folded some of those into
    # one address while emitting others separately. Assign greedily so each of
    # our functions claims a distinct shipped address rather than all of them
    # crediting the first hit.
    #
    # A library function with more than one hit (Lith.lib's CBaseList::Delete
    # has the same bytes as an engine list's Remove at 0x00442c90 in
    # lithtech.exe) takes the fresh hit nearest the addresses its library's
    # single-hit functions anchored: a library is linked as a block, so its
    # own copy is the one beside its siblings, not the first one in .text.
    matched = {}
    tiny = {}          # our functions with too few unmasked bytes to anchor
    lib_hits = {}
    lib_range = LIB_RANGE.get(label, {})
    for n, (code, mask) in funcs.items():
        if n in from_lib and len(code) - len(mask) >= 8:
            lib_hits[n] = list(find(text, tva, code, mask))
            rng = lib_range.get(n.split(":", 1)[0])
            if rng:
                # outside the library's established block: not the library
                lib_hits[n] = [h for h in lib_hits[n] if rng[0] <= h < rng[1]]
    start_set = set(starts)
    anchors = {}
    for n, h in lib_hits.items():
        if len(h) == 1:
            anchors.setdefault(n.split(":", 1)[0], []).append(h[0])
    placed = set()
    # A generic shape (a d3d.ren thiscall thunk recurs at 19 addresses)
    # otherwise claims the FIRST free hit and leaves the address it was
    # written for unscored, or a same-shape neighbour takes it first. A symbol
    # named for its address (`sub_<addr>`, the d3d convention) whose bytes
    # match AT that address is placed there before anything else.
    # (d3d.ren: also a name tools/d3dcheck.py's NAMED table maps -- the
    # engine-named getters d3d_IsIn3D and d3d_IsInOptimized2D are the same
    # six bytes, and by first-fit each was credited at the other's address.)
    for n, (code, mask) in sorted(funcs.items()):
        a = _own_addr(label, n, funcs)
        if a is None or n in from_lib or len(code) - len(mask) < 8:
            continue
        off = a - tva
        for c, m in [(code, mask)] + ([unpadded[n]] if n in unpadded else []):
            if a in matched or off < 0 or off + len(c) > len(text):
                continue
            if all(i in m or text[off + i] == c[i] for i in range(len(c))):
                matched[a] = len(c)
                PLACED_SYMS[(label, a)] = n
                placed.add(n)
    for n, (code, mask) in sorted(funcs.items()):
        if n in placed:
            continue
        if len(code) - len(mask) < 8:
            tiny[n] = (code, mask)
            continue
        hits = lib_hits[n] if n in lib_hits else list(find(text, tva, code, mask))
        if not hits and n in unpadded:
            # icl pads a COMDAT with multi-byte lea-nops, not 0x90/0xCC, and
            # parse_obj_masked strips only those two; where the shipped image
            # has different bytes after the function the padded body is
            # found nowhere. Try the body without it.
            code, mask = unpadded[n]
            if len(code) - len(mask) >= 8:
                hits = list(find(text, tva, code, mask))
        if not hits:
            continue
        fresh = [h for h in hits if h not in matched]
        # A hit that is not an enumerated function start is a byte-run
        # inside some other function (a short body's masked shape recurring
        # in a neighbour's tail): only take it when nothing better exists.
        # input_GetInputMgr's 13 bytes otherwise claim 0x00408b9e, the else
        # arm of 0x00408b80, and leave its own 0x00439fb0 unscored.
        if len(fresh) > 1:
            at_start = [h for h in fresh if h in start_set]
            if at_start:
                fresh = at_start
        anc = anchors.get(n.split(":", 1)[0]) if n in from_lib else None
        if anc and len(fresh) > 1:
            fresh.sort(key=lambda h: min(abs(h - a) for a in anc))
        at = (fresh or hits)[0]
        matched[at] = len(code)
        PLACED_SYMS[(label, at)] = n

    # A library linked into an engine image is one contiguous block of it, so
    # a library function's hit strictly inside another matched function is a
    # coincidence (StdLith's 8-byte CAbstractIO::Open "matches" the middle of
    # rezmgr2's CRezDirX720::RemoveRezInternal in server.dll). Drop those,
    # and let the library's unanchorable functions claim addresses by shape
    # only inside the span its anchored functions occupy -- outside it,
    # StdLith's $E initializer thunks otherwise claim the engine's own.
    lib_span = {}
    for at in [a for a in matched if PLACED_SYMS.get((label, a)) in from_lib]:
        if any(b < at < b + m for b, m in matched.items() if b != at):
            del matched[at]
            del PLACED_SYMS[(label, at)]
            continue
        mod, sym = PLACED_SYMS[(label, at)].split(":", 1)
        if "?$" in sym or sym.startswith("??_G"):
            # template instances and deleting dtors are emitted by every
            # object that uses them and fold anywhere; they place nothing
            continue
        l0, h0 = lib_span.get(mod, (at, at))
        lib_span[mod] = (min(l0, at), max(h0, at + matched[at]))
    for n in [n for n in tiny if n in from_lib]:
        mod = n.split(":", 1)[0]
        if mod not in lib_span and mod not in lib_range:
            del tiny[n]

    # Second oracle: our own whole-exe link. exematch has to mask relocation
    # bytes because it compares an unlinked .obj, which leaves the global
    # initializer COMDATs — two unmasked opcodes each — unverifiable. In a
    # linked image those relocations are resolved, so a function built purely
    # from relative displacements can be compared byte for byte with nothing
    # masked at all. That is a stronger claim than the masked match, not a
    # weaker one; it just only applies to functions with no absolute operands.
    linked = None
    lp = os.path.join(REPO, LINKED.get(label, ""))
    if LINKED.get(label) and os.path.exists(lp):
        linked = EnumImage(lp).text

    lo, hi = APP_REGION.get(label, (img.tlo, img.thi))

    # Enumeration finds some starts that are not functions: exception-handler
    # funclets and jump islands inside a function we have already matched, and
    # the odd 1-byte fragment left by the padding carve. Anything strictly
    # inside a matched function's span is one of those, not a target.
    inside = set()
    for a, n in matched.items():
        for b in starts:
            if a < b < a + n:
                inside.add(b)
    starts = [a for a in starts if a not in inside]

    # Global-initializer COMDATs ($E thunks, the ??__E constructors and the
    # atexit registrations) are almost entirely relocation bytes: a `call
    # rel32; jmp rel32` pair leaves two unmasked opcodes, which anchors
    # nowhere. They cannot be verified by this oracle even when the source is
    # certainly right. Claim them by shape instead — each of our unanchorable
    # functions takes one distinct shipped address whose bytes agree outside
    # the mask — and score them "small": reconstructed, not verified.
    if linked is not None:
        for a in starts:
            if a in matched or not in_app(label, a, lo, hi):
                continue
            n = sizes.get(a, 0)
            if n < 1:
                continue
            b = text[a - tva : a - tva + n]
            if b and b in linked:
                matched[a] = n

    small = set()
    placed_small = set()

    def own_first(n):
        """d3d.ren: a symbol named for its address (`sub_<addr>`) tries that
        address first, as the exact-match placement above does -- a 6-byte
        `mov <g>,%eax ; ret` or a 1-byte `ret` fits every other one of its
        shape, and taking the first in address order steals a sibling's."""
        a0 = _own_addr(label, n, funcs) if label == "d3d.ren" else None
        if a0 is not None and a0 in starts:
            return [a0] + [a for a in starts if a != a0]
        return starts

    for n, (code, mask) in sorted(tiny.items()):
        # a COMDAT is padded out to alignment; the function is what precedes it
        end = len(code)
        while end > 0 and code[end - 1] in (0x90, 0xCC):
            end -= 1
        for a in own_first(n):
            if a in matched or a in small or not in_app(label, a, lo, hi):
                continue
            if n in from_lib:
                mod = n.split(":", 1)[0]
                l0, h0 = lib_range.get(mod) or lib_span[mod]
                if not l0 <= a < h0:
                    continue
            if sizes.get(a) != end:            # same length, exactly
                continue
            off = a - tva
            if all(i in mask or text[off + i] == code[i] for i in range(end)):
                small.add(a)
                placed_small.add(n)
                SMALL_SYMS[(label, a)] = n
                break

    # d3d.ren is icl's, which pads with multi-byte lea-nops (d3dcheck.PADS)
    # as well as 0x90: a 1-byte `ret` followed by `90 8d b4 26 ..` keeps a
    # 16-byte extent once only 0x90/0xCC are stripped, so the same-length
    # test above can never pass for it. A second pass, for the tiny d3d
    # functions the first one left unplaced only, compares the bodies with
    # that padding stripped on both sides.
    if label == "d3d.ren":
        for n, (code, mask) in sorted(tiny.items()):
            if n in placed_small or n in from_lib:
                continue
            end = len(strip_pad(bytes(code)))
            for a in own_first(n):
                if a in matched or a in small or not in_app(label, a, lo, hi):
                    continue
                off = a - tva
                body = strip_pad(bytes(text[off:off + sizes.get(a, 0)]))
                if len(body) != end:
                    continue
                if all(i in mask or text[off + i] == code[i]
                       for i in range(end)):
                    small.add(a)
                    break

    # A function we wrote that does not match still corresponds to a shipped
    # address, and leaving that address in "todo" says we have no source for it
    # when we do. Pair each unmatched object function with its best remaining
    # candidate: same length to within 15%, and at least half the unmasked
    # bytes equal. This only ever relabels todo -> near; it never claims a
    # match.
    def tail(buf, i, j):
        """The callee-pop epilogue: `ret` or `ret imm16`, whichever ends it.

        Never a relocation, and a hard ABI fact -- two functions that pop
        different argument bytes cannot be the same function however much
        of their prologue coincides. On small functions that is most of the
        discriminating signal there is, because masking removes the rest.
        """
        if j - i >= 3 and buf[j - 3] == 0xC2:
            return bytes(buf[j - 3:j])
        if j - i >= 1 and buf[j - 1] == 0xC3:
            return b"\xc3"
        return None

    paired = {}
    rejected = 0
    if not from_lib is None:
        for n, (code, mask) in sorted(funcs.items()):
            if len(code) - len(mask) < 8 or n in from_lib:
                continue
            if list(find(text, tva, code, mask)):
                continue
            end = len(code)
            while end > 0 and code[end - 1] in (0x90, 0xCC):
                end -= 1
            best, best_rank, best_ok = None, 0.0, False
            # d3d.ren: a symbol named for its address is tried THERE first,
            # as own_first does for the exact and small placements -- a 5.8 KB
            # clipper whose bytes differ mostly by register choice agrees with
            # its own address on ~12% of them and with an unrelated big
            # function on 16%, and the ranking below paired it with neither.
            a0 = _own_addr(label, n, funcs) if label == "d3d.ren" else None
            cand = starts
            if a0 is not None and a0 in start_set:
                cand = [a0] + [a for a in starts if a != a0]
            for a in cand:
                if a in matched or a in paired or not in_app(label, a, lo, hi):
                    continue
                m = sizes.get(a, 0)
                if not m or abs(m - end) > 0.15 * max(m, end):
                    continue
                off, k = a - tva, min(m, end)
                pfx = 0
                while pfx < k and (pfx in mask or text[off + pfx] == code[pfx]):
                    pfx += 1
                same = sum(1 for i in range(k)
                           if i not in mask and text[off + i] == code[i])
                den = sum(1 for i in range(k) if i not in mask) or 1
                agree = same / den
                rank = max(agree, pfx / max(k, 1))
                if rank > best_rank:
                    # either a shared 16-byte opening — far more than two
                    # unrelated functions with similar prologues manage — or
                    # most of the body agreeing, with the length lining up too.
                    # Both then have to survive the epilogue and have enough
                    # unmasked bytes for the ratio above to have meant
                    # anything: a dozen agreeing bytes out of a dozen is a
                    # calling convention, not a function.
                    ok = (pfx >= 16 or agree >= 0.5) and den >= 12
                    # The shipped row's extent runs to the next start, so on
                    # d3d.ren it still carries icl's multi-byte lea/mov nop
                    # padding (`8b f6`, `8d 76 00` ...): `tail` then saw no
                    # `ret` and a correct body could never pair (0x10077680,
                    # 0x10078910). Strip that padding first, as `code` is.
                    m_end = off + m
                    if label == "d3d.ren":
                        m_end = off + len(strip_pad(text[off:off + m]))
                    ours, theirs = tail(code, 0, end), tail(text, off, m_end)
                    if ok and ours is not None and ours != theirs:
                        ok = False
                        rejected += 1
                    best, best_rank, best_ok = a, rank, ok
                if a == a0:
                    ok0 = (pfx >= 16 or agree >= 0.5) and den >= 12
                    o0, t0 = tail(code, 0, end), tail(text, off, off + m)
                    if ok0 and (o0 is None or o0 == t0):
                        best, best_rank, best_ok = a, rank, True
                        break
            if best is not None and best_ok:
                paired[best] = n
                NEAR_PAIRS.append((label, best, sizes.get(best, 0), n, end))
    if rejected:
        print(f"  {label}: {rejected} candidate pairing(s) rejected on "
              f"argument-pop mismatch", file=sys.stderr)

    rows = []
    # Functions we wrote source for that did NOT match: instruction-equivalent
    # near-matches, or code simply not linked into this image. Keyed by symbol
    # since we have no address for them.
    claimed = set(paired.values())
    for n, (code, mask) in sorted(funcs.items()):
        if len(code) - len(mask) < 8 or n in from_lib:
            # lib functions are scored in their own row; the linker drops the
            # ones this image never calls, and a dropped function is not a
            # near-match
            continue
        if n in claimed:
            # already counted once, at the shipped address it pairs with
            continue
        if not list(find(text, tva, code, mask)) and not (
                n in unpadded and list(find(text, tva, *unpadded[n]))):
            # UNPLACED, not "near". This function's bytes occur NOWHERE in
            # the image, so it is not a shipped function missed by a little
            # -- it is usually code this image never links. lithtech.exe
            # compiles 44 of the server's translation units, so every
            # server-only function lands here. Counting these as shipped
            # functions inflated the denominator by 1548 rows and made four
            # FINISHED libraries read 87-98% when they are at 100%: their
            # entire "near" column was this. Reported in its own column and
            # excluded from funcs/bytes_total.
            rows.append((label, n, len(code), "unplaced"))
    for a in sorted(set(starts) | set(matched)):
        size = matched.get(a, sizes.get(a, 0))
        if a in matched:
            rows.append((label, "sub_%x" % a, size, "identical"))
        elif a in paired:
            rows.append((label, "sub_%x" % a, size, "near"))
        elif a in small:
            rows.append((label, "sub_%x" % a, size, "small"))
        elif in_app(label, a, lo, hi):
            # SPLIT: "todo" now means nobody has looked at it. An address we
            # have evidence of working on -- written and unplaceable, decoded,
            # or recorded as not-work -- is "attempted" and counted apart.
            rows.append((label, "sub_%x" % a, size,
                         "attempted" if a in _touched_index(label) else "todo"))
        else:
            rows.append((label, "sub_%x" % a, size, "crt"))
    return rows


def _resources(path):
    """{(type, name, language): data} of a PE image's resources."""
    d = open(path, "rb").read()
    pe = struct.unpack_from("<I", d, 0x3C)[0]
    nsec = struct.unpack_from("<H", d, pe + 6)[0]
    optsz = struct.unpack_from("<H", d, pe + 20)[0]
    rva = struct.unpack_from("<I", d, pe + 24 + 96 + 2 * 8)[0]
    secs = []
    for i in range(nsec):
        s = pe + 24 + optsz + i * 40
        vs, va, rs, rp = struct.unpack_from("<IIII", d, s + 8)
        secs.append((va, max(vs, rs), rp))

    def off(r):
        for va, sz, rp in secs:
            if va <= r < va + sz:
                return r - va + rp
        raise ValueError("resource RVA 0x%x outside every section" % r)

    base = off(rva)

    def name(v):
        if v & 0x80000000:
            o = base + (v & 0x7FFFFFFF)
            n = struct.unpack_from("<H", d, o)[0]
            return d[o + 2:o + 2 + 2 * n].decode("utf-16-le")
        return v

    def walk(o, path):
        out = {}
        n = sum(struct.unpack_from("<HH", d, o + 12))
        for i in range(n):
            nm, ptr = struct.unpack_from("<II", d, o + 16 + 8 * i)
            key = path + (name(nm),)
            if ptr & 0x80000000:
                out.update(walk(base + (ptr & 0x7FFFFFFF), key))
            else:
                drva, dsz = struct.unpack_from("<II", d, base + ptr)
                out[key] = d[off(drva):off(drva) + dsz]
        return out

    return walk(base, ())


def rows_for_resources(label, shipped, ours):
    """A resource-only image: one row per shipped resource, `type/name/lang`,
    sized by its data. identical = our resource has the same bytes; near =
    we have it, but its bytes differ; todo = we have none by that key."""
    theirs = _resources(shipped)
    mine = _resources(ours) if os.path.exists(ours) else {}
    rows = []
    for key in sorted(theirs, key=str):
        data = theirs[key]
        if key not in mine:
            status = "todo"
        elif mine[key] == data:
            status = "identical"
        else:
            status = "near"
        rows.append((label, "rsrc:" + "/".join(str(k) for k in key), len(data), status))
    return rows


def calltarget_starts(path):
    """Function starts for a symbol-less PE: call rel32 targets + exports.

    More accurate than padding-carve for binaries with no inter-function
    padding (server.dll, d3d.ren). Returns (sorted starts, text_lo, text_hi).
    """
    d = open(path, "rb").read()
    pe = struct.unpack_from("<I", d, 0x3C)[0]
    nsec = struct.unpack_from("<H", d, pe + 6)[0]
    optsz = struct.unpack_from("<H", d, pe + 20)[0]
    base = struct.unpack_from("<I", d, pe + 24 + 28)[0]
    so = pe + 24 + optsz
    tlo = thi = trp = 0
    for i in range(nsec):
        s = so + i * 40
        name = d[s : s + 8].rstrip(b"\0").decode("latin-1")
        vs, va, rs, rp = struct.unpack_from("<IIII", d, s + 8)
        if name == ".text":
            tlo, thi, trp = base + va, base + va + vs, rp
    text = d[trp : trp + (thi - tlo)]
    starts = set()
    for off in range(len(text) - 5):
        if text[off] == 0xE8:
            rel = struct.unpack_from("<i", text, off + 1)[0]
            tgt = tlo + off + 5 + rel
            if tlo <= tgt < thi:
                starts.add(tgt)
    return sorted(starts), tlo, thi


def rows_for_engine(label, path):
    """Unstarted engine binary: full function enumeration (tools/enumfuncs.py).

    Same enumerator as the started binaries, so the denominators are directly
    comparable: call targets + data pointers (vtables) + exports + code after a
    ret that nothing references. Padding-carving is NOT used — it works on
    lithtech.exe but fails on server.dll/d3d.ren, which have almost no
    inter-function padding.
    """
    img = EnumImage(os.path.join(REPO, path))
    starts, _src = enumerate_functions(img)
    rows = []
    for i, a in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else img.thi
        lo, hi = a - img.tlo, min(end - img.tlo, len(img.text))
        while hi > lo and img.text[hi - 1] in (0x90, 0xCC):
            hi -= 1
        rows.append((label, "sub_%x" % a, hi - lo, "todo"))
    return rows



# ---------------------------------------------------------------------------
# TOUCHED: is there evidence in the tree that THIS shipped address has been
# worked on? `todo` only ever meant "no exact match and no successful
# pairing", so a function we have written whose bytes drifted past the
# pairing tolerance was counted identically to code nobody has ever looked
# at. Measured across the three engine images, ~85% of `todo` is the former
# -- which made the scoreboard's red bar a picture of imperfection rather
# than of remaining effort.
#
# Evidence accepted, cheapest first: the address is cited anywhere in our
# sources, or the decoded map places a symbol there, or a screen-exclude
# row records it as not-work.
_TOUCHED = {}

_PHANTOM = {}


def _phantom_index(label=None):
    """Addresses PROVEN not to be functions, to be dropped from the count.

    The enumeration is wrong in both directions. Now that the remaining
    work is small, the error matters: a row that is an
    operand byte, an interior jump target, alignment padding or an ASCII
    accident is not code, and leaving it in the denominator makes 100%
    unreachable BY CONSTRUCTION rather than by any fact about the source.

    Only rows whose screen-exclude reason opens with an explicit NOT A
    FUNCTION / NOT GAME CODE marker are dropped. "Already written" rows are
    NOT dropped -- those are real functions that happen to be credited at a
    sibling address, and removing them would flatter the figure. The marker
    has to be at the START of the reason so that a reason merely MENTIONING
    the phrase cannot silently delete a row.
    """
    if label in _PHANTOM:
        return _PHANTOM[label]
    out = set()
    for f in _exclude_files(label):
        try:
            for r in csv.DictReader(open(f)):
                a = (r.get("addr") or "").strip().lower()
                why = (r.get("reason") or "").strip()
                if not a.startswith("0x"):
                    continue
                if re.match(r"(?i)^(not a function|not game code)\b", why):
                    try:
                        out.add(int(a, 16))
                    except ValueError:
                        pass
        except OSError:
            pass
    _PHANTOM[label] = out
    return out


_CITED = None


def _cited_addrs():
    """Every 0x... cited in our sources: one scan, shared by all images."""
    global _CITED
    if _CITED is not None:
        return _CITED
    blob = []
    for base in ("src/engine", "src/lithshared"):
        for root, _, fs in os.walk(base):
            for f in fs:
                if f.endswith((".cpp", ".h")) and "NAMEMAP" not in f:
                    try:
                        blob.append(open(os.path.join(root, f),
                                         errors="ignore").read())
                    except OSError:
                        pass
    text = "\n".join(blob)
    _CITED = set(re.findall(r"0x[0-9a-fA-F]{6,8}", text))
    return _CITED


def _touched_index(label=None):
    if label in _TOUCHED:
        return _TOUCHED[label]
    # The game DLLs are built from Monolith's own source, which cites no
    # retail address; the addresses src/engine/ cites are the engine
    # images', and server.dll's and d3d.ren's share the game DLLs' range
    # (0x10000000+), so counting them here would mark game functions
    # "attempted" by coincidence.
    if label in GAME_LABELS:
        _TOUCHED[label] = set()
        return _TOUCHED[label]
    addrs = set(_cited_addrs())
    for extra in glob.glob(DATA + "/*-decoded-map.csv") + \
                 _exclude_files(label):
        try:
            for r in csv.DictReader(open(extra)):
                a = (r.get("addr") or "").strip().lower()
                if a.startswith("0x"):
                    addrs.add(a)
        except OSError:
            pass
    # normalise to a set of ints so 0x456400 and 0x00456400 are one key
    out = set()
    for a in addrs:
        try:
            out.add(int(a, 16))
        except ValueError:
            pass
    _TOUCHED[label] = out
    return out


def main():
    os.chdir(REPO)

    # A missing reference image would silently drop its target from the
    # totals; refuse instead.
    missing = [n for n in ("NOLF.exe", "lithtech.exe", "server.dll", "d3d.ren",
                           "CShell.dll", "Object.lto", "CRes.dll")
               if not os.path.exists(os.path.join(REFERENCE, n))]
    if missing:
        sys.exit("progress.py: %s missing from %s/ -- run `make progress`, or "
                 "tools/discextract.py reference <isodir> %s"
                 % (", ".join(missing), REFERENCE, REFERENCE))
    # ... and so would a missing build: every function would read "todo".
    unbuilt = [d for d in ("src/engine/launcher", "src/engine/server",
                           "src/engine/lithtech", "src/engine/d3d")
               if not glob.glob(d + "/Release/*.obj")] + \
              [g[2] for g in GAME if not glob.glob(g[2] + "/*.obj")] + \
              [GAME_RES[2]] * (not os.path.exists(GAME_RES[2])) + \
              [p for _l, p in LIBS if not os.path.exists(p)]
    if unbuilt:
        sys.exit("progress.py: no build output in %s -- run `make` first"
                 % ", ".join(unbuilt))

    libdir = shipped_lib_dir()
    all_rows = []
    if libdir:
        for label, ours in LIBS:
            all_rows += rows_for_lib(label, ours, libdir)
    else:
        print("progress.py: NOLF_SOURCE not set -- the %d lithshared libraries "
              "are not scored (see progress/README.md)" % len(LIBS),
              file=sys.stderr)

    # every reconstructed launcher TU (stdafx.obj is empty -- not real
    # reconstruction)
    exe_objs = sorted(
        p for p in glob.glob("src/engine/launcher/Release/*.obj")
        if os.path.basename(p) != "stdafx.obj"
    )
    # RegMgr is linked into NOLF.exe from our own reconstructed lithshared lib,
    # so its functions in the exe are ours too
    exe_objs.append("src/lithshared/out/RegMgr.lib")
    all_rows += rows_for_image("NOLF.exe", REFERENCE + "/NOLF.exe", exe_objs)

    started = ({label for label, _ in LIBS} if libdir else set()) | {"NOLF.exe"}

    STARTED_ENGINE = {
        # StdLith.lib is linked into server.dll whole (0x1003d170..0x1003e0ef,
        # see APP_REGION); like RegMgr.lib in NOLF.exe, its functions there
        # are ours, from our reconstructed lib.
        "server.dll": (REFERENCE + "/server.dll",
                       sorted(glob.glob("src/engine/server/Release/*.obj"))
                       + ["src/lithshared/out/StdLith.lib",
                          # Lith.lib (basehash/baselist/virtlist): the
                          # APP_ISLANDS block above the CRT
                          "src/lithshared/out/Lith.lib"]),
        # The server's sources compiled a second time, with lithtech.exe's
        # own compiler revision and flags (build/engine.sh), plus its own TUs.
        "lithtech.exe": (REFERENCE + "/lithtech.exe",
                         sorted(glob.glob("src/engine/lithtech/Release/*.obj"))
                         # the same prebuilt Lith.lib as server.dll: its
                         # APP_ISLANDS block plus baselist at 0x0048de30
                         + ["src/lithshared/out/Lith.lib",
                            # StdLith.lib, confined to its block by LIB_RANGE
                            "src/lithshared/out/StdLith.lib"]),
        # Renderer-specific source only (icl 4.0; build/engine.sh).
        "d3d.ren": (REFERENCE + "/d3d.ren",
                    sorted(glob.glob("src/engine/d3d/Release/*.obj"))),
    }
    for label, path in ENGINE:
        se = STARTED_ENGINE.get(label)
        objs = [o for o in (se[1] if se else []) if os.path.exists(o)]
        if se and objs:
            all_rows += rows_for_image(label, se[0], objs)
            started.add(label)
        elif os.path.exists(path):
            all_rows += rows_for_engine(label, path)

    # the game DLLs: our objects plus the lithshared libraries each links
    for label, path, objdir, libs in GAME:
        objs = sorted(glob.glob(objdir + "/*.obj")) + \
            ["src/lithshared/out/%s.lib" % l for l in libs
             if os.path.exists("src/lithshared/out/%s.lib" % l)]
        all_rows += rows_for_image(label, path, objs)
        started.add(label)
    # scored, but not "started": resource data is not code, so it stays out
    # of the total (and the README lists it below it)
    all_rows += rows_for_resources(*GAME_RES)

    # --todo[=<target>] (or --attempted, --near, --small): the rows of one
    # status. The table gives a number and the number alone is not a
    # worklist.
    want_cls = None
    for a in sys.argv[1:]:
        for cls in ("todo", "attempted", "near", "small"):
            if a == "--" + cls or a.startswith("--" + cls + "="):
                want_cls = cls
    if want_cls:
        pick = ""
        for a in sys.argv[1:]:
            if a.startswith("--" + want_cls + "="):
                pick = a.split("=", 1)[1]
        sel = [r for r in all_rows
               if r[3] == want_cls and (not pick or r[0].startswith(pick))]
        sel.sort(key=lambda r: (r[0], -r[2]))
        print(f"{'target':<14} {'symbol':<16} {'bytes':>7}")
        for r in sel:
            print(f"{r[0]:<14} {r[1]:<16} {r[2]:>7}")
        print(f"\n{len(sel)} row(s), {sum(r[2] for r in sel)} bytes")
        return

    want = None
    for a in sys.argv[1:]:
        if a == "--nears":
            want = ""
        elif a.startswith("--nears="):
            want = a.split("=", 1)[1]
    if want is not None:
        sel = [r for r in NEAR_PAIRS if not want or r[0].startswith(want)]
        sel.sort(key=lambda r: (abs(r[4] - r[2]), r[2]))
        print(f"{'target':<14} {'shipped':<12} {'ship':>6} {'ours':>6} "
              f"{'delta':>6}  symbol")
        for label, addr, ssize, sym, osize in sel:
            print(f"{label:<14} 0x{addr:08x} {ssize:>6} {osize:>6} "
                  f"{osize - ssize:>+6}  {sym}")
        print(f"\n{len(sel)} near-matches"
              f" ({sum(1 for r in sel if r[4] == r[2])} already size-exact)")
        return

    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, "functions.csv"), "w", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["target", "function", "size", "status"])
        for r in sorted(all_rows, key=lambda r: (r[0], r[1])):
            w.writerow(r)

    # aggregate
    targets = {}
    for label, name, size, status in all_rows:
        t = targets.setdefault(label, {"identical": 0, "near": 0, "todo": 0,
                                       "attempted": 0,
                                       "small": 0, "crt": 0, "crt_bytes": 0,
                                       "unplaced": 0, "unplaced_bytes": 0,
                                       "bytes_identical": 0, "bytes_total": 0,
                                       "funcs": 0})
        t[status] += 1
        if status == "crt":          # toolchain code: visible, not a target
            t["crt_bytes"] += size
            continue
        if status == "unplaced":     # our code, not in THIS image: not a
            t["unplaced_bytes"] += size    # shipped function, so not a
            continue                       # denominator either
        t["funcs"] += 1
        t["bytes_total"] += size
        if status == "identical":
            t["bytes_identical"] += size

    for label, t in targets.items():
        t["started"] = label in started

    summary = {"libraries_scored": bool(libdir), "targets": targets}
    # "started" total = the reconstruction scoreboard (precise function metric)
    tot = {"identical": 0, "near": 0, "todo": 0, "attempted": 0,
           "small": 0, "funcs": 0,
           "unplaced": 0, "unplaced_bytes": 0,
           "bytes_identical": 0, "bytes_total": 0}
    for label, t in targets.items():
        if t["started"]:
            for k in tot:
                tot[k] += t[k]
    summary["total"] = tot

    with open(os.path.join(OUT, "summary.json"), "w") as f:
        json.dump(summary, f, indent=2, sort_keys=True)
        f.write("\n")

    print(f"{'target':<18} {'ident':>6} {'near':>5} {'small':>6} "
          f"{'attem':>6} {'todo':>5} "
          f"{'funcs':>6} {'unpl':>5} {'bytes id':>10} {'bytes tot':>10}  {'%':>5}")
    print("-" * 99)
    order = ([l for l, _ in LIBS] if libdir else []) + ["NOLF.exe"] + \
            [l for l, _ in ENGINE if l in targets and targets[l]["started"]] + \
            [g[0] for g in GAME]
    for label in order:
        if label not in targets:
            continue
        t = targets[label]
        pct = 100 * t["bytes_identical"] / max(t["bytes_total"], 1)
        print(f"{label:<18} {t['identical']:>6} {t['near']:>5} {t['small']:>6} "
              f"{t.get('attempted', 0):>6} {t['todo']:>5} "
              f"{t['funcs']:>6} {t.get('unplaced', 0):>5} "
              f"{t['bytes_identical']:>10} "
              f"{t['bytes_total']:>10}  {pct:>4.1f}%")
    r = targets.get(GAME_RES[0])
    if r:
        pct = 100 * r["bytes_identical"] / max(r["bytes_total"], 1)
        print(f"{GAME_RES[0]:<18} {r['identical']:>6} {r['near']:>5} "
              f"{r['small']:>6} {r.get('attempted', 0):>6} {r['todo']:>5} "
              f"{r['funcs']:>6} {r.get('unplaced', 0):>5} "
              f"{r['bytes_identical']:>10} {r['bytes_total']:>10}  {pct:>4.1f}%"
              "  (resources, not code: not in ALL)")
    print("-" * 99)
    pct = 100 * tot["bytes_identical"] / max(tot["bytes_total"], 1)
    print(f"{'ALL':<18} {tot['identical']:>6} {tot['near']:>5} "
          f"{tot['small']:>6} {tot.get('attempted', 0):>6} {tot['todo']:>5} "
          f"{tot['funcs']:>6} "
          f"{tot['unplaced']:>5} "
          f"{tot['bytes_identical']:>10} {tot['bytes_total']:>10}  {pct:>4.1f}%")
    if not libdir:
        print("(the lithshared libraries are not included: NOLF_SOURCE is not set)")
    print(f"wrote {OUT}/functions.csv, {OUT}/summary.json")


if __name__ == "__main__":
    main()
