#!/usr/bin/env python3
"""The modern build's game-data edits, applied at build time.

The modern build (`make modern`, NOLFSRC_MODERN) needs some game data changed
as well as code: an attribute value, a new menu's strings. No game data is
committed to this repository, so a change is committed as a RECIPE, and this
applies it to the data the build already took from the discs:

  src/modern/data/<path>.patch   a unified diff against the retail file at
                                 <path> in the rez stack (e.g.
                                 ATTRIBUTES/WEAPONS.txt.patch)
  src/modern/data/<path>.from    one line, "<archive> <path in it>": that
                                 retail file, from that archive, installed
                                 at <path> (e.g. an original-edition texture
                                 the GOTY archives override, under a new name)
  src/modern/data/files/<path>   a new file, written for this project,
                                 installed as-is at <path>
  src/modern/data/<path>.bytes   byte edits of a binary retail file (e.g. a
                                 palette entry of a .pcx): one edit per line,
                                 "<offset> <old hex> <new hex>", the offset
                                 in hex or decimal and negative to count
                                 from the end; the old bytes must match
  src/modern/data/gamedir/...    the same four kinds, for files the engine
                                 reads from the game directory itself rather
                                 than from the rez archives (defctrls.cfg):
                                 a .patch applies to the game directory's
                                 file, whose retail original is kept beside
                                 it as <file>.retail

The results are written into the game directory's custom/ rez directory,
which the engine searches after the four .rez archives, so they override the
retail copies (gamedir/ recipes: into the game directory). The retail file a
patch applies to is the one the game itself would load: the last archive in
nolfcmds.txt's -rez order that holds it. custom/MODERN-DATA.txt lists what
was written; a file a previous run wrote and this one did not is removed (a
game-directory file is put back to its .retail original).

  tools/moddata.py apply <gamedir> [recipes-dir]    (build/variant.sh runs it)
  tools/moddata.py show  <gamedir> <path>           print a file as the game
                                                    sees it (to diff against)
  tools/moddata.py diff  <gamedir> <path> <edited>  write a recipe from an
                                                    edited copy

A patch must apply exactly (context lines included), or the build fails:
data that drifted from what a patch expects means the patch is wrong.
"""
import difflib
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from rezextract import Rez  # noqa: E402

LIST = "MODERN-DATA.txt"


def rez_stack(gamedir):
    """The game's .rez archives in load order, from nolfcmds.txt."""
    cmds = open(os.path.join(gamedir, "nolfcmds.txt"), errors="replace").read()
    stack = []
    for name in re.findall(r"-rez\s+(\S+)", cmds):
        p = os.path.join(gamedir, name)
        if name.lower().endswith(".rez") and os.path.isfile(p):
            stack.append(p)
    return stack


class Data:
    """The game's data as the engine sees it, before our edits."""

    def __init__(self, gamedir):
        self.index = {}                 # lower-case path -> (rez, pos, size, path)
        self.rez = {}
        for p in rez_stack(gamedir):    # later archives override earlier ones
            r = self.rez[p] = Rez(p)
            for path, pos, size in r.walk():
                self.index[path.lower()] = (p, pos, size, path)

    def read_from(self, archive, path):
        """A file from one named archive (e.g. nolf2.rez), not the stack."""
        for p, r in self.rez.items():
            if os.path.basename(p).lower() == archive.lower():
                want = path.replace("\\", "/").lower()
                for q, pos, size in r.walk():
                    if q.lower() == want:
                        r.f.seek(pos)
                        return r.f.read(size), q
                return None, None
        sys.exit(f"moddata: no archive {archive} in the game's rez stack")

    def read(self, path):
        hit = self.index.get(path.replace("\\", "/").lower())
        if hit is None:
            return None, None
        p, pos, size, real = hit
        f = self.rez[p].f
        f.seek(pos)
        return f.read(size), real


def apply_patch(text, patch, name):
    """Apply a unified diff to text exactly; keep the file's line endings."""
    crlf = "\r\n" in text
    lines = text.replace("\r\n", "\n").split("\n")
    out, i = [], 0
    hunks = re.split(r"^@@ ", patch, flags=re.M)[1:]
    if not hunks:
        sys.exit(f"moddata: {name}: no hunks")
    for h in hunks:
        m = re.match(r"-(\d+)(?:,(\d+))? \+\d+(?:,\d+)? @@.*\n", h)
        if not m:
            sys.exit(f"moddata: {name}: bad hunk header")
        start = int(m.group(1)) - (0 if m.group(2) == "0" else 1)
        body = h[m.end():].split("\n")
        if body and body[-1] == "":
            body.pop()
        old = [l[1:] for l in body if l[:1] in (" ", "-")]
        new = [l[1:] for l in body if l[:1] in (" ", "+")]
        if lines[start:start + len(old)] != old:
            sys.exit(f"moddata: {name}: a hunk at line {start + 1} does not match "
                     f"the retail file -- the patch is out of date")
        out += lines[i:start] + new
        i = start + len(old)
    out += lines[i:]
    res = "\n".join(out)
    return res.replace("\n", "\r\n") if crlf else res


def apply_bytes(raw, recipe, name):
    """Apply a .bytes recipe: "<offset> <old hex> <new hex>" per line."""
    data = bytearray(raw)
    for n, line in enumerate(open(recipe).read().split("\n"), 1):
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        f = line.split()
        if len(f) != 3:
            sys.exit(f"moddata: {name}: line {n}: expected \"<offset> <old hex> <new hex>\"")
        off = int(f[0], 0)
        old, new = bytes.fromhex(f[1]), bytes.fromhex(f[2])
        if len(old) != len(new):
            sys.exit(f"moddata: {name}: line {n}: old and new differ in length")
        if off < 0:
            off += len(data)
        if off < 0 or data[off:off + len(old)] != old:
            sys.exit(f"moddata: {name}: line {n}: the retail bytes do not match "
                     f"-- the recipe is out of date")
        data[off:off + len(new)] = new
    return bytes(data)


def recipes(rdir):
    """(target, game path, kind, recipe file) for every recipe; target is
    "custom" or "gamedir"."""
    out = []
    for root, _, fs in os.walk(rdir):
        for f in sorted(fs):
            p = os.path.join(root, f)
            rel = os.path.relpath(p, rdir).replace("\\", "/")
            target = "custom"
            if rel.startswith("gamedir/"):
                target, rel = "gamedir", rel[len("gamedir/"):]
            if rel.startswith("files/"):
                out.append((target, rel[len("files/"):], "file", p))
            elif f.endswith(".patch"):
                out.append((target, rel[:-len(".patch")], "patch", p))
            elif f.endswith(".from"):
                out.append((target, rel[:-len(".from")], "from", p))
            elif f.endswith(".bytes"):
                out.append((target, rel[:-len(".bytes")], "bytes", p))
    return sorted(out)


def gamedir_retail(gamedir, path):
    """A game-directory file's retail original: <file>.retail, kept the first
    time a recipe replaces the file."""
    dest = os.path.join(gamedir, path)
    keep = dest + ".retail"
    if not os.path.exists(keep):
        if not os.path.exists(dest):
            return None
        os.replace(dest, keep)
    return open(keep, "rb").read()


def cmd_apply(gamedir, rdir):
    data = Data(gamedir)
    custom = os.path.join(gamedir, "custom")
    written = []
    for target, path, kind, recipe in recipes(rdir):
        if kind == "file":
            content, real = open(recipe, "rb").read(), path
            if target == "gamedir":
                gamedir_retail(gamedir, path)
        elif kind == "from":
            spec = open(recipe).read().split()
            if len(spec) != 2:
                sys.exit(f"moddata: {recipe}: expected \"<archive> <path in it>\"")
            content, _ = data.read_from(spec[0], spec[1])
            if content is None:
                sys.exit(f"moddata: {recipe}: no {spec[1]} in {spec[0]}")
            real = path
            if target == "gamedir":
                gamedir_retail(gamedir, path)
        else:
            if target == "gamedir":
                raw, real = gamedir_retail(gamedir, path), path
            else:
                raw, real = data.read(path)
            if raw is None:
                sys.exit(f"moddata: {path}: no retail file to patch")
            if kind == "bytes":
                content = apply_bytes(raw, recipe, path)
            else:
                text = raw.decode("latin-1")
                content = apply_patch(text, open(recipe, encoding="latin-1").read(),
                                      path).encode("latin-1")
        root = gamedir if target == "gamedir" else custom
        dest = os.path.join(root, real)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        open(dest, "wb").write(content)
        entry = ("gamedir:" if target == "gamedir" else "") + real
        written.append(entry)
        print(f"   {'' if target == 'gamedir' else 'custom/'}{real}  <- {os.path.relpath(recipe)}")
    # undo what an earlier run wrote and this one did not
    lp = os.path.join(custom, LIST)
    os.makedirs(custom, exist_ok=True)
    if os.path.exists(lp):
        for old in open(lp).read().split("\n"):
            if not old or old in written:
                continue
            if old.startswith("gamedir:"):
                f = os.path.join(gamedir, old[len("gamedir:"):])
                if os.path.exists(f + ".retail"):
                    os.replace(f + ".retail", f)
                elif os.path.isfile(f):
                    os.remove(f)
            elif os.path.isfile(os.path.join(custom, old)):
                os.remove(os.path.join(custom, old))
    with open(lp, "w") as f:
        f.write("".join(p + "\n" for p in written))
    print(f"   {len(written)} data file(s) from {os.path.relpath(rdir)}")


def cmd_show(gamedir, path):
    raw, real = Data(gamedir).read(path)
    if raw is None:
        sys.exit(f"moddata: {path}: not in the game's rez archives")
    sys.stdout.buffer.write(raw)


def cmd_diff(gamedir, path, edited):
    raw, real = Data(gamedir).read(path)
    if raw is None:
        sys.exit(f"moddata: {path}: not in the game's rez archives")
    a = raw.decode("latin-1").replace("\r\n", "\n").split("\n")
    b = open(edited, "rb").read().decode("latin-1").replace("\r\n", "\n").split("\n")
    d = difflib.unified_diff(a, b, f"a/{real}", f"b/{real}", lineterm="", n=3)
    sys.stdout.write("\n".join(d) + "\n")


def main():
    a = sys.argv[1:]
    if len(a) >= 2 and a[0] == "apply":
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        cmd_apply(a[1], a[2] if len(a) > 2 else os.path.join(here, "src/modern/data"))
    elif len(a) == 3 and a[0] == "show":
        cmd_show(a[1], a[2])
    elif len(a) == 4 and a[0] == "diff":
        cmd_diff(a[1], a[2], a[3])
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
