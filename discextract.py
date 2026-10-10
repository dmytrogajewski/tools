#!/usr/bin/env python3
"""Take what the build needs off the two NOLF GOTY disc images.

  discextract.py resources  <isodir> <gamedir>   game DATA only (no binary)
  discextract.py thirdparty <isodir> <gamedir>   Miles + Bink, nothing else
  discextract.py launcherres <isodir> <resdir>   the launcher's art and
                                                 sound, out of the retail
                                                 NOLF.exe's resources
  discextract.py audit      <gamedir>            every PE file in a game
                                                 directory, with its origin

<isodir> holds NOLF1-CD1.iso and NOLF1-CD2.iso.

`resources` is build/all.sh's first step. It copies every file of the disc's
Game/ directory that is not a Windows executable image (PE files are
recognised by their content, the "MZ" header, not by their name), the movie
files, and the four .rez archives the retail startup.txt stacks. The archives
are game data, but three of them also carry the retail game DLLs
(CSHELL.dll, OBJECT.lto, CRES.dll, SRES.dll): those entries are overwritten
with zeros in our copy -- the archive stays valid, the binary is gone -- so
the only game code in the directory is what we compiled (installed in the
custom/ rez directory, which the engine searches last-first).

`thirdparty` copies the licensed middleware the engine imports and we have
no source for: Miles Sound System (mss32.dll and its .flt / .m3d / .asi /
.as6 providers) and Bink (binkw32.dll). These are the only binaries from the
discs that go into the game, and they come only from here.

`launcherres` writes the six binary resources launcher.rc includes from
src/engine/launcher/res/ -- the splash bitmap and its palette, the music,
the icon and two cursors. They are Monolith's art and sound, not code, so
they are not in this repository: they are read out of the resource section
of the retail NOLF.exe on the disc (the image is parsed in memory, never
written anywhere or run) and written back as the .bmp / .pal / .wav / .ico
/ .cur files rc.exe compiles into our NOLF.exe.

Every file the steps write is recorded in <gamedir>/MANIFEST.txt
(`<origin>\\t<path>`); build/all.sh adds the binaries it built, and `audit`
lists every PE file in the directory with its origin, failing on any it
cannot account for -- or on any PE file still inside a .rez archive.
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from isoextract import Iso  # noqa: E402
from rezextract import Rez  # noqa: E402

DISC1, DISC2 = "NOLF1-CD1.iso", "NOLF1-CD2.iso"

# The rez stack of the retail GOTY install, as nolfcmds.txt names it
# (startup.txt lists more names; the ones not on the discs are skipped).
REZ = [
    (DISC2, "Data/NOLF.REZ",     "nolf.rez"),
    (DISC1, "Data/NOLF2.REZ",    "nolf2.rez"),
    (DISC1, "Data/nolfu003.rez", "nolfu003.rez"),
    (DISC1, "Data/NOLFGOTY.REZ", "nolfgoty.rez"),
]

# THIRD-PARTY RUNTIME: lithtech.exe and NOLF.exe import mss32.dll (Miles),
# and Miles loads its filter/3D/codec providers from the game directory;
# lithtech.exe loads binkw32.dll for the movies. No source exists for any of
# them, and the game cannot start without Miles. Nothing else from the discs
# may be a binary.
THIRD_PARTY_NAMES = {"mss32.dll", "binkw32.dll"}
THIRD_PARTY_EXTS = {".flt", ".m3d", ".asi", ".as6"}

# Configs the build writes itself rather than copying (see build/all.sh).
NOT_COPIED = {"autoexec.cfg"}


def is_third_party(name):
    n = name.lower()
    return n in THIRD_PARTY_NAMES or os.path.splitext(n)[1] in THIRD_PARTY_EXTS


class Disc:
    def __init__(self, isodir, name):
        path = os.path.join(isodir, name)
        if not os.path.exists(path):
            sys.exit(f"missing {path} -- see ISOS/SHA256SUMS")
        self.name = name
        self.iso = Iso(path)
        self.files = {p: (ext, sz) for p, ext, sz in self.iso.walk()}

    def find(self, path):
        for p in self.files:
            if p.lower() == path.lower():
                return p
        sys.exit(f"{self.name}: no {path}")

    def head(self, path, n=2):
        ext, sz = self.files[path]
        self.iso.f.seek(ext * 2048)
        return self.iso.f.read(min(n, sz))

    def copy(self, path, dest):
        ext, sz = self.files[path]
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".part")
        with open(tmp, "wb") as out:
            self.iso.read_file(ext, sz, out)
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


def cmd_resources(isodir, gamedir):
    gamedir = Path(gamedir)
    gamedir.mkdir(parents=True, exist_ok=True)
    man = Manifest(gamedir)
    skipped = []
    for disc_name in (DISC2,):
        d = Disc(isodir, disc_name)
        for p in sorted(d.files):
            top, _, rest = p.partition("/")
            if top not in ("Game", "Movies") or not rest or "/" in rest:
                continue
            rel = rest if top == "Game" else f"Movies/{rest}"
            if rest.lower() in NOT_COPIED:
                continue
            if d.head(p) == b"MZ":
                if not is_third_party(rest):
                    skipped.append(p)
                continue          # third-party binaries: `thirdparty` step
            dest = gamedir / rel
            if not dest.exists() or dest.stat().st_size != d.files[p][1]:
                d.copy(p, dest)
            man.add(rel, f"data {disc_name}:{p}")
    discs = {}
    for disc_name, src, name in REZ:
        d = discs.get(disc_name) or discs.setdefault(disc_name, Disc(isodir, disc_name))
        p = d.find(src)
        dest = gamedir / name
        if not dest.exists() or dest.stat().st_size != d.files[p][1]:
            print(f"== {disc_name}:{p} -> {name}")
            d.copy(p, dest)
        for ename, _pos, sz in scrub_rez(dest):
            print(f"   {name}: retail binary {ename} ({sz} bytes) zeroed")
        man.add(name, f"data {disc_name}:{p} (game binaries inside zeroed)")
    man.save()
    for p in skipped:
        print(f"   not copied (a retail binary): {DISC2}:{p}")


def cmd_thirdparty(isodir, gamedir):
    gamedir = Path(gamedir)
    gamedir.mkdir(parents=True, exist_ok=True)
    man = Manifest(gamedir)
    d = Disc(isodir, DISC2)
    n = 0
    for p in sorted(d.files):
        top, _, rest = p.partition("/")
        if top != "Game" or "/" in rest or not is_third_party(rest):
            continue
        dest = gamedir / rest
        if not dest.exists() or dest.stat().st_size != d.files[p][1]:
            d.copy(p, dest)
        man.add(rest, f"THIRD-PARTY {DISC2}:{p}")
        n += 1
    man.save()
    print(f"== third-party runtime: {n} files (Miles, Bink)")


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


def cmd_launcherres(isodir, resdir):
    import io
    d = Disc(isodir, DISC2)
    p = d.find("Game/NOLF.exe")
    buf = io.BytesIO()
    ext, sz = d.files[p]
    d.iso.read_file(ext, sz, buf)
    res = pe_resources(buf.getvalue())
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
    print(f"== {DISC2}:{p} resources -> {resdir}/ ({', '.join(out)})")


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
    order = {"built": 0, "THIRD-PARTY": 1}
    rows.sort(key=lambda r: (order.get(r[0].split()[0], 2), r[1].lower()))
    print(f"{'size':>9}  {'file':<24} origin")
    for origin, rel, sz in rows:
        print(f"{sz:>9}  {rel:<24} {origin}")
    built = sum(1 for r in rows if r[0].startswith("built"))
    third = sum(1 for r in rows if r[0].startswith("THIRD-PARTY"))
    print(f"{len(rows)} PE files: {built} built from our source, "
          f"{third} third-party runtime, {bad} unaccounted for")
    return 1 if bad else 0


def main():
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    cmd = sys.argv[1]
    if cmd == "resources" and len(sys.argv) == 4:
        cmd_resources(sys.argv[2], sys.argv[3])
    elif cmd == "thirdparty" and len(sys.argv) == 4:
        cmd_thirdparty(sys.argv[2], sys.argv[3])
    elif cmd == "launcherres" and len(sys.argv) == 4:
        cmd_launcherres(sys.argv[2], sys.argv[3])
    elif cmd == "audit" and len(sys.argv) == 3:
        sys.exit(cmd_audit(sys.argv[2]))
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
