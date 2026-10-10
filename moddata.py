#!/usr/bin/env python3
"""The modern build's game-data edits, applied at build time.

The modern build (`make modern`, NOLFSRC_MODERN) needs some game data changed
as well as code: an attribute value, a new menu's strings. No game data is
committed to this repository, so a change is committed as a RECIPE, and this
applies it to the data the build already took from the discs:

  src/modern/data/<path>.patch   a unified diff against the retail file at
                                 <path> in the rez stack (e.g.
                                 ATTRIBUTES/WEAPONS.txt.patch)
  src/modern/data/files/<path>   a new file, written for this project,
                                 installed as-is at <path>

Each result is written into the game directory's custom/ rez directory,
which the engine searches after the four .rez archives, so it overrides the
retail copy. The retail file a patch applies to is the one the game itself
would load: the last archive in nolfcmds.txt's -rez order that holds it.
custom/MODERN-DATA.txt lists what was written; files a previous run wrote
and this one did not are removed.

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


def recipes(rdir):
    """(game path, kind, recipe file) for every recipe."""
    out = []
    for root, _, fs in os.walk(rdir):
        for f in sorted(fs):
            p = os.path.join(root, f)
            rel = os.path.relpath(p, rdir).replace("\\", "/")
            if rel.startswith("files/"):
                out.append((rel[len("files/"):], "file", p))
            elif f.endswith(".patch"):
                out.append((rel[:-len(".patch")], "patch", p))
    return sorted(out)


def cmd_apply(gamedir, rdir):
    data = Data(gamedir)
    custom = os.path.join(gamedir, "custom")
    written = []
    for path, kind, recipe in recipes(rdir):
        if kind == "file":
            content = open(recipe, "rb").read()
            real = path
        else:
            raw, real = data.read(path)
            if raw is None:
                sys.exit(f"moddata: {path}: not in the game's rez archives")
            text = raw.decode("latin-1")
            content = apply_patch(text, open(recipe, encoding="latin-1").read(),
                                  path).encode("latin-1")
        dest = os.path.join(custom, real)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        open(dest, "wb").write(content)
        written.append(real)
        print(f"   custom/{real}  <- {os.path.relpath(recipe)}")
    # remove what an earlier run wrote and this one did not
    lp = os.path.join(custom, LIST)
    if os.path.exists(lp):
        for old in open(lp).read().split("\n"):
            if old and old not in written and os.path.isfile(os.path.join(custom, old)):
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
