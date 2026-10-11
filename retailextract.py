#!/usr/bin/env python3
"""Take what the build needs from the installed retail game.

  retailextract.py resources   <retaildir> <gamedir>  game DATA only (no binary)
  retailextract.py thirdparty  <retaildir> <gamedir>  Miles + Bink, nothing else
  retailextract.py openruntime <extdir> <gamedir>     the modern build's open
                                                      replacements for them
                                                      (build/ext.sh's output)
  retailextract.py launcherres <retaildir> <resdir>   the launcher's art and
                                                      sound, out of the retail
                                                      NOLF.exe's resources
  retailextract.py goty        <retaildir> <outdir>   the Game of the Year text
                                                      our CRes.dll is built
                                                      with, out of the retail
                                                      one
  retailextract.py reference   <retaildir> <refdir>   the retail engine images
                                                      and game DLLs, for
                                                      scoring only
  retailextract.py audit       <gamedir>              every PE file in a game
                                                      directory, with its
                                                      origin

<retaildir> is the Game of the Year Edition (Europe) as the rez/ submodule
installs it from the two disc images (rez/install-games.sh nolf-eu-goty:
rez/games/nolf-eu-goty/, see build/retail.sh). It is only read: nothing in it
is run, and the build never writes to it.

`resources` is build/all.sh's first step. It copies every file of the play
disc's Game/ directory that is not a Windows executable image (PE files are
recognised by their content, the "MZ" header, not by their name), the movie
files, and the four .rez archives the retail startup.txt stacks. (rez/
installs Game/ into the directory's top level, beside the .rez archives and
EReg/, which the build does not use.) The archives
are game data, but three of them also carry the retail game DLLs
(CSHELL.dll, OBJECT.lto, CRES.dll, SRES.dll): those entries are overwritten
with zeros in our copy -- the archive stays valid, the binary is gone -- so
the only game code in the directory is what we compiled (installed in the
custom/ rez directory, which the engine searches last-first).

`thirdparty` copies the licensed middleware the engine imports and we have
no source for: Miles Sound System (mss32.dll and its .flt / .m3d / .asi /
.as6 providers) and Bink (binkw32.dll). These are the only binaries from the
retail game that go into ours, and they come only from here.

`openruntime` is the modern build's step 2 instead (NOLF_MIDDLEWARE=open,
build/all.sh): it removes any retail Miles / Bink file a previous build left
in the game directory and installs OpenMiles' mss32.dll and our FFmpeg-based
binkw32.dll, which build/ext.sh built from ext/ into <extdir>. OpenMiles
decodes and mixes everything itself, so no Miles provider file goes in.

`launcherres` writes the six binary resources launcher.rc includes from
src/engine/launcher/res/ -- the splash bitmap and its palette, the music,
the icon and two cursors. They are Monolith's art and sound, not code, so
they are not in this repository: they are read out of the resource section
of the retail NOLF.exe (the image is parsed in memory, never
written anywhere or run) and written back as the .bmp / .pal / .wav / .ico
/ .cur files rc.exe compiles into our NOLF.exe.

`goty` writes the two things the retail Game of the Year CRES.dll (in
NOLFGOTY.REZ) has and Monolith's v1.003 source of it does not: the
string tables of the Game of the Year content (string IDs 30000-30095: the
"Rest and Relaxation" mission's name, briefing, objectives, intel and
subtitles), as goty.rc, and its credits text, as credits.txt. build/build.sh
compiles them into our CRes.dll with the release's own resource script, so its
resources are the retail ones. The retail DLL is read in memory, never
written anywhere or run; the text stays out of this repository.

`reference` copies the retail lithtech.exe, server.dll, d3d.ren, NOLF.exe and
ltmsg.dll, and the Game of the Year CShell.dll, Object.lto and CRes.dll out of
NOLFGOTY.REZ, to a reference directory (build/reference.sh: out/reference/). They
are what tools/progress.py, `build/engine.sh ... verify` and the
reconstruction tools compare our objects against; the build never reads
them, and nothing in that directory is ever run or goes into a game
directory.

Every file the steps write is recorded in <gamedir>/MANIFEST.txt
(`<origin>\\t<path>`); build/all.sh adds the binaries it built, and `audit`
lists every PE file in the directory with its origin, failing on any it
cannot account for -- or on any PE file still inside a .rez archive.
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import shutil

from rezextract import Rez  # noqa: E402

# The rez stack of the retail GOTY install, as nolfcmds.txt names it
# (startup.txt lists more names; the ones the GOTY install lacks are
# skipped): the installed archive, and its name in the game directory.
REZ = [
    ("NOLF.REZ",     "nolf.rez"),
    ("NOLF2.REZ",    "nolf2.rez"),
    ("nolfu003.rez", "nolfu003.rez"),
    ("NOLFGOTY.REZ", "nolfgoty.rez"),
]

# THIRD-PARTY RUNTIME: lithtech.exe and NOLF.exe import mss32.dll (Miles),
# and Miles loads its filter/3D/codec providers from the game directory;
# lithtech.exe loads binkw32.dll for the movies. No source exists for any of
# them, and the game cannot start without Miles. Nothing else from the retail
# game may be a binary.
THIRD_PARTY_NAMES = {"mss32.dll", "binkw32.dll"}
THIRD_PARTY_EXTS = {".flt", ".m3d", ".asi", ".as6"}

# The retail images the checks compare against (`reference`).
REFERENCE = ["lithtech.exe", "server.dll", "d3d.ren", "NOLF.exe", "ltmsg.dll"]
# ... and the retail game DLLs, which the retail game only holds inside the .rez
# archives (the game loads them from there). There are three generations:
# NOLF2.REZ has the original release's, nolfu003.rez the 1.003 update's, and
# NOLFGOTY.REZ the Game of the Year Edition's -- the copies the GOTY install
# runs (it is last in the rez stack) and the ones our build replaces.
# Monolith's source release builds the GOTY ones: scored against each,
# Object.lto has 89% of its bytes identical to GOTY's and 55% to 1.003's.
REFERENCE_GAME = [("CSHELL.dll", "CShell.dll"), ("OBJECT.lto", "Object.lto"),
                  ("CRES.dll", "CRes.dll")]

# Configs the build writes itself rather than copying (see build/all.sh).
NOT_COPIED = {"autoexec.cfg"}


def is_third_party(name):
    n = name.lower()
    return n in THIRD_PARTY_NAMES or os.path.splitext(n)[1] in THIRD_PARTY_EXTS


class Retail:
    """The installed retail game, read only. rez/ records where each of its
    files came from in its MANIFEST.txt (`<path>\t<image>.iso:<disc path>`);
    `game(p)` is whether a file is one of the play disc's Game/ directory,
    which rez/ installs into the top level."""

    def __init__(self, root):
        self.root = Path(root)
        man = self.root / "MANIFEST.txt"
        if not man.exists():
            sys.exit(f"missing {man} -- install the retail game first "
                     "(build/retail.sh)")
        self.name = self.root.name
        self.origin = {}
        for line in man.read_text().splitlines():
            if "\t" in line and not line.startswith("#"):
                rel, origin = line.split("\t", 1)
                self.origin[rel] = origin.split(".iso:", 1)[-1]

    def find(self, path):
        for p in self.origin:
            if p.lower() == path.lower():
                return p
        sys.exit(f"{self.root}: no {path}")

    def game(self, p):
        return "/" not in p and self.origin[p].split("/", 1)[0].lower() == "game"

    def size(self, p):
        return (self.root / p).stat().st_size

    def head(self, p, n=2):
        with open(self.root / p, "rb") as f:
            return f.read(n)

    def read(self, p):
        return (self.root / p).read_bytes()

    def copy(self, p, dest):
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".part")
        shutil.copyfile(self.root / p, tmp)
        tmp.replace(dest)


class Manifest:
    def __init__(self, gamedir):
        self.path = Path(gamedir) / "MANIFEST.txt"
        self.rows = {}
        if self.path.exists():
            for line in self.path.read_text().splitlines():
                if "\t" in line and not line.startswith("#"):
                    origin, p = line.split("\t", 1)
                    self.rows[p] = origin

    def add(self, relpath, origin):
        self.rows[relpath] = origin

    def save(self):
        with open(self.path, "w") as f:
            f.write("# origin<TAB>path, one row per file build/all.sh put here\n")
            for p in sorted(self.rows, key=str.lower):
                f.write(f"{self.rows[p]}\t{p}\n")


def rez_pe_entries(path):
    """(name, pos, size) of every entry in a .rez that is a PE image."""
    rez = Rez(path)
    out = []
    for name, pos, sz in list(rez.walk()):
        if sz < 2:
            continue
        rez.f.seek(pos)
        if rez.f.read(2) == b"MZ":
            out.append((name, pos, sz))
    rez.f.close()
    return out


def scrub_rez(path):
    """Overwrite every PE entry of a .rez with zeros. Idempotent."""
    hits = rez_pe_entries(path)
    with open(path, "r+b") as f:
        for name, pos, sz in hits:
            f.seek(pos)
            f.write(b"\0" * sz)
    return hits


def cmd_resources(retaildir, gamedir):
    gamedir = Path(gamedir)
    gamedir.mkdir(parents=True, exist_ok=True)
    man = Manifest(gamedir)
    r = Retail(retaildir)
    skipped = []
    for p in sorted(r.origin):
        top, _, rest = p.partition("/")
        if r.game(p):
            rel = p
        elif top == "Movies" and rest and "/" not in rest:
            rel = p
        else:
            continue
        if p.lower() in NOT_COPIED:
            continue
        if r.head(p) == b"MZ":
            if not is_third_party(p):
                skipped.append(p)
            continue          # third-party binaries: `thirdparty` step
        dest = gamedir / rel
        if not dest.exists() or dest.stat().st_size != r.size(p):
            r.copy(p, dest)
        man.add(rel, f"data {r.name}:{p}")
    for src, name in REZ:
        p = r.find(src)
        dest = gamedir / name
        if not dest.exists() or dest.stat().st_size != r.size(p):
            print(f"== {r.name}:{p} -> {name}")
            r.copy(p, dest)
        for ename, _pos, sz in scrub_rez(dest):
            print(f"   {name}: retail binary {ename} ({sz} bytes) zeroed")
        man.add(name, f"data {r.name}:{p} (game binaries inside zeroed)")
    man.save()
    for p in skipped:
        print(f"   not copied (a retail binary): {r.name}:{p}")


def cmd_thirdparty(retaildir, gamedir):
    gamedir = Path(gamedir)
    gamedir.mkdir(parents=True, exist_ok=True)
    man = Manifest(gamedir)
    r = Retail(retaildir)
    n = 0
    for p in sorted(r.origin):
        if not r.game(p) or not is_third_party(p):
            continue
        dest = gamedir / p
        if not dest.exists() or dest.stat().st_size != r.size(p):
            r.copy(p, dest)
        man.add(p, f"THIRD-PARTY {r.name}:{p}")
        n += 1
    man.save()
    print(f"== third-party runtime: {n} files (Miles, Bink)")


# The modern build's replacements (build/ext.sh), and where each comes from.
OPEN_RUNTIME = [
    ("mss32.dll",   "ext/openmiles: OpenMiles, Miles 6.0 exports"),
    ("binkw32.dll", "src/modern/binkw32 + ext/ffmpeg: our Bink player"),
]


def cmd_openruntime(extdir, gamedir):
    gamedir = Path(gamedir)
    gamedir.mkdir(parents=True, exist_ok=True)
    man = Manifest(gamedir)
    for f in sorted(gamedir.iterdir()):
        if f.is_file() and is_third_party(f.name):
            f.unlink()
            man.rows.pop(f.name, None)
            print(f"   removed {f.name}")
    for name, origin in OPEN_RUNTIME:
        shutil.copyfile(Path(extdir) / name, gamedir / name)
        man.add(name, f"EXT {origin} (build/ext.sh)")
        print(f"   {name}  <-  {origin}")
    man.save()
    print(f"== open runtime: {len(OPEN_RUNTIME)} files (OpenMiles, Bink on FFmpeg)")


# --- launcher resources -----------------------------------------------------

RT_CURSOR, RT_BITMAP, RT_ICON, RT_GROUP_CURSOR, RT_GROUP_ICON = 1, 2, 3, 12, 14


def pe_resources(img):
    """{(type, name): data} of a PE image's resource section (first language
    of each). Types and names are ints or upper-case strings."""
    import struct
    pe = struct.unpack_from("<I", img, 0x3C)[0]
    nsec = struct.unpack_from("<H", img, pe + 6)[0]
    optsz = struct.unpack_from("<H", img, pe + 20)[0]
    rva, _sz = struct.unpack_from("<II", img, pe + 24 + 96 + 2 * 8)   # dir 2
    secs = []
    for i in range(nsec):
        o = pe + 24 + optsz + 40 * i
        vsz, va, rawsz, raw = struct.unpack_from("<IIII", img, o + 8)
        secs.append((va, max(vsz, rawsz), raw))

    def off(r):
        for va, sz, raw in secs:
            if va <= r < va + sz:
                return raw + r - va
        raise ValueError(f"rva {r:#x} not in any section")

    base = off(rva)

    def name(v):
        if v & 0x80000000:
            o = base + (v & 0x7FFFFFFF)
            n = struct.unpack_from("<H", img, o)[0]
            return img[o + 2:o + 2 + 2 * n].decode("utf-16-le").upper()
        return v

    def entries(o):
        nn, ni = struct.unpack_from("<HH", img, o + 12)
        for i in range(nn + ni):
            yield struct.unpack_from("<II", img, o + 16 + 8 * i)

    out = {}
    for t, tsub in entries(base):
        for n, nsub in entries(base + (tsub & 0x7FFFFFFF)):
            for _lang, leaf in entries(base + (nsub & 0x7FFFFFFF)):
                drva, dsz = struct.unpack_from("<II", img, base + leaf)
                out[(name(t), name(n))] = img[off(drva):off(drva) + dsz]
                break
    return out


def bmp_file(dib):
    import struct
    hsz, bpp, used = struct.unpack_from("<I", dib, 0)[0], struct.unpack_from("<H", dib, 14)[0], \
        struct.unpack_from("<I", dib, 32)[0]
    colors = used or (1 << bpp if bpp <= 8 else 0)
    return b"BM" + struct.pack("<IHHI", 14 + len(dib), 0, 0, 14 + hsz + 4 * colors) + dib


def icon_file(res, group):
    import struct
    g = res[(RT_GROUP_ICON, group)]
    _r, typ, n = struct.unpack_from("<HHH", g, 0)
    head, body, at = struct.pack("<HHH", 0, 1, n), b"", 6 + 16 * n
    for i in range(n):
        w, h, cc, rsv, planes, bpp, size, rid = struct.unpack_from("<BBBBHHIH", g, 6 + 14 * i)
        data = res[(RT_ICON, rid)]
        head += struct.pack("<BBBBHHII", w, h, cc, rsv, planes, bpp, len(data), at + len(body))
        body += data
    return head + body


def cursor_file(res, group):
    import struct
    g = res[(RT_GROUP_CURSOR, group)]
    _r, typ, n = struct.unpack_from("<HHH", g, 0)
    head, body, at = struct.pack("<HHH", 0, 2, n), b"", 6 + 16 * n
    for i in range(n):
        w, h2, _planes, _bpp, _size, rid = struct.unpack_from("<HHHHIH", g, 6 + 14 * i)
        data = res[(RT_CURSOR, rid)]
        hx, hy = struct.unpack_from("<HH", data, 0)
        dib = data[4:]
        head += struct.pack("<BBBBHHII", w, h2 // 2, 0, 0, hx, hy, len(dib), at + len(body))
        body += dib
    return head + body


def cmd_launcherres(retaildir, resdir):
    r = Retail(retaildir)
    p = r.find("NOLF.exe")
    res = pe_resources(r.read(p))
    out = {
        "game1.bmp": bmp_file(res[(RT_BITMAP, "GAME1")]),
        "game1.pal": res[("PALETTE", "GAME1")],
        "game1.wav": res[("WAVE", "GAME1")],
        "nolf.ico": icon_file(res, "ICON"),
        "webhand.cur": cursor_file(res, "WEBHAND"),
        "webclick.cur": cursor_file(res, "WEBCLICK"),
    }
    resdir = Path(resdir)
    resdir.mkdir(parents=True, exist_ok=True)
    for name, data in out.items():
        dest = resdir / name
        if not dest.exists() or dest.read_bytes() != data:
            dest.write_bytes(data)
    print(f"== {r.name}:{p} resources -> {resdir}/ ({', '.join(out)})")


# The GOTY string blocks: string IDs 30000-30095 are blocks 1876-1881 (a block
# holds IDs 16*(n-1) .. 16*(n-1)+15). These are exactly what the retail DLL has
# and ours lacks (tools/rsrcdiff.py); refuse a retail DLL where that is not so.
GOTY_STRING_BLOCKS = range(1876, 1882)
RT_STRING = 6


def rc_string(s):
    """A string as an rc.exe string literal (code page 1252)."""
    out = []
    for ch in s:
        o = ord(ch)
        if ch == '"':
            out.append('""')
        elif ch == "\\":
            out.append("\\\\")
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        elif o < 32:
            out.append("\\%03o" % o)
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def cmd_goty(retaildir, outdir):
    import struct
    r = Retail(retaildir)
    p = r.find("NOLFGOTY.REZ")
    rez = Rez(str(r.root / p))
    img = None
    for path, pos, size in rez.walk():
        if path.lower() == "cres.dll":
            rez.f.seek(pos)
            img = rez.f.read(size)
    if img is None:
        sys.exit(f"retailextract: no CRES.dll in {r.name}:{p}")
    res = pe_resources(img)
    lines = ["// Generated by tools/retailextract.py goty from the retail Game of the",
             "// Year CRES.dll -- do not edit, not committed.", "",
             "STRINGTABLE DISCARDABLE", "BEGIN"]
    n = 0
    for blk in GOTY_STRING_BLOCKS:
        data = res.get((RT_STRING, blk))
        if data is None:
            sys.exit(f"retailextract: the retail CRES.dll has no string block {blk}")
        o = 0
        for i in range(16):
            ln = struct.unpack_from("<H", data, o)[0]
            o += 2
            s = data[o:o + 2 * ln].decode("utf-16-le")
            o += 2 * ln
            if ln:
                lines.append("    %d %s" % ((blk - 1) * 16 + i, rc_string(s)))
                n += 1
    lines += ["END", ""]
    credits = res.get(("TEXT", "CREDITS"))
    if credits is None:
        sys.exit("retailextract: the retail CRES.dll has no CREDITS text")
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    rc = "\r\n".join(lines).encode("cp1252")
    for name, data in (("goty.rc", rc), ("credits.txt", credits)):
        dest = outdir / name
        if not dest.exists() or dest.read_bytes() != data:
            dest.write_bytes(data)
    print(f"== {r.name}:{p} CRES.dll -> {outdir}/goty.rc ({n} strings), credits.txt")


def cmd_reference(retaildir, refdir):
    refdir = Path(refdir)
    refdir.mkdir(parents=True, exist_ok=True)
    r = Retail(retaildir)
    for name in REFERENCE:
        p = r.find(name)
        dest = refdir / name
        r.copy(p, dest)
        print(f"== {r.name}:{p} -> {dest}")
    p = r.find("NOLFGOTY.REZ")
    rez = Rez(str(r.root / p))
    found = {path.lower(): (pos, size) for path, pos, size in rez.walk()}
    for name, out in REFERENCE_GAME:
        if name.lower() not in found:
            sys.exit(f"retailextract: no {name} in {r.name}:{p}")
        pos, size = found[name.lower()]
        rez.f.seek(pos)
        (refdir / out).write_bytes(rez.f.read(size))
        print(f"== {r.name}:{p}:{name} -> {refdir / out}")
    rez.f.close()


def cmd_audit(gamedir):
    gamedir = Path(gamedir)
    man = Manifest(gamedir)
    bad = 0
    rows = []
    for root, _dirs, files in os.walk(gamedir, followlinks=True):
        for fn in files:
            full = Path(root) / fn
            rel = str(full.relative_to(gamedir))
            with open(full, "rb") as f:
                head = f.read(2)
            if head == b"MZ":
                origin = man.rows.get(rel, "UNKNOWN")
                if origin == "UNKNOWN" or origin.startswith("data "):
                    bad += 1
                rows.append((origin, rel, full.stat().st_size))
            elif fn.lower().endswith(".rez"):
                for ename, _pos, sz in rez_pe_entries(full):
                    rows.append(("INSIDE A REZ ARCHIVE", f"{rel}:{ename}", sz))
                    bad += 1
    order = {"built": 0, "EXT": 1, "THIRD-PARTY": 2}
    rows.sort(key=lambda r: (order.get(r[0].split()[0], 3), r[1].lower()))
    print(f"{'size':>9}  {'file':<24} origin")
    for origin, rel, sz in rows:
        print(f"{sz:>9}  {rel:<24} {origin}")
    built = sum(1 for r in rows if r[0].startswith("built"))
    ext = sum(1 for r in rows if r[0].startswith("EXT"))
    third = sum(1 for r in rows if r[0].startswith("THIRD-PARTY"))
    print(f"{len(rows)} PE files: {built} built from our source, "
          + (f"{ext} open-source middleware built from ext/, " if ext else "")
          + f"{third} third-party runtime, {bad} unaccounted for")
    return 1 if bad else 0


def main():
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    cmd = sys.argv[1]
    if cmd == "resources" and len(sys.argv) == 4:
        cmd_resources(sys.argv[2], sys.argv[3])
    elif cmd == "thirdparty" and len(sys.argv) == 4:
        cmd_thirdparty(sys.argv[2], sys.argv[3])
    elif cmd == "openruntime" and len(sys.argv) == 4:
        cmd_openruntime(sys.argv[2], sys.argv[3])
    elif cmd == "launcherres" and len(sys.argv) == 4:
        cmd_launcherres(sys.argv[2], sys.argv[3])
    elif cmd == "goty" and len(sys.argv) == 4:
        cmd_goty(sys.argv[2], sys.argv[3])
    elif cmd == "reference" and len(sys.argv) == 4:
        cmd_reference(sys.argv[2], sys.argv[3])
    elif cmd == "audit" and len(sys.argv) == 3:
        sys.exit(cmd_audit(sys.argv[2]))
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
