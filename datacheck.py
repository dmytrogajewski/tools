#!/usr/bin/env python3
"""The data check: the text in our images' data that is not the retail text.

tools/progress.py compares code (with the addresses the linker writes
masked) and resources; neither reads the rest of an image's data. This
reads it: per image, the text in every section that is neither code nor
.reloc nor .rsrc (the resources are scored resource by resource), with the
import and export tables read as what they are -- "import KERNEL32.dll
GetTickCount", "export name server.DLL" -- rather than as bytes, and
compares the two images' text as multisets: a string the retail image holds
and ours does not is a "retail" difference, one only ours holds an "ours"
difference.

A string is a run of printable bytes ending in a NUL, at least five long and
mostly letters (the filter that keeps float constants and table bytes out).
A run not preceded by a NUL is preceded by other data, whose last bytes can
be printable by chance (the "5?" of a float before "Error: ..."): such a
run is cut back to its first 4-byte-aligned character, where the compiler
puts a literal.

The images are progress/targets.py's: every target with a `shipped` image
and a build of ours, once per image.

Differences that are known and meant -- each with its reason -- are listed
in progress/data-known.txt; anything else is NEW and fails `make progress`:

  datacheck.py check     report every image; exit 1 on a new difference
  datacheck.py update    the same, then drop from progress/data-known.txt
                         every entry that no longer differs (`make
                         progress-update`); entries are only ever added by
                         hand, with their reason
  datacheck.py show [image]   list every difference, known or not
  datacheck.py baseline [--image=<image>] <reason>
                         append every NEW difference to progress/data-known.txt
                         under `# <reason>`: a project taking the check up
                         records what is there as UNREVIEWED, so that the
                         check fails on anything added from then on while
                         the list stays to be worked through

progress/data-known.txt: `<image><TAB><retail|ours><TAB><string>`, the
string written with Python's unicode_escape (so a backslash is doubled);
`# ...` lines are the reason for the entries that follow them, until the
next one (`## ...` lines are the file's own header). An entry repeated n
times allows the string n times; the string `*` allows every difference of
its image on that side (an image whose data is known not to match as a
whole).
"""
import collections
import os
import re
import struct
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
KNOWN = "progress/data-known.txt"
REFERENCE = "out/reference"



def images():
    """[(label, retail image, ours)]: every image progress/targets.py names
    that the build has made, once each (an image's `:rsrc` target shares
    it), labelled by the retail file's name."""
    import tgt as tgt_mod
    out, seen = [], set()
    for t in tgt_mod.cfg.TARGETS:
        ref = t.get("shipped")
        if not ref or ref in seen or not t.get("built"):
            continue
        ours = tgt_mod.built(t)
        if not ours:
            continue
        seen.add(ref)
        out.append((os.path.basename(ref), ref, ours))
    return out


IMAGES = []

IMAGE_SCN_MEM_EXECUTE = 0x20000000
RUN = re.compile(rb"[\x20-\x7e\t\r\n]{5,}(?=\x00)")


class Image:
    def __init__(self, path):
        d = self.d = open(path, "rb").read()
        pe = struct.unpack_from("<I", d, 0x3C)[0]
        nsec = struct.unpack_from("<H", d, pe + 6)[0]
        optsz = struct.unpack_from("<H", d, pe + 20)[0]
        self.dirs = [struct.unpack_from("<II", d, pe + 24 + 96 + 8 * i)
                     for i in range(16)]
        self.secs = []
        for i in range(nsec):
            o = pe + 24 + optsz + 40 * i
            name = d[o:o + 8].rstrip(b"\0").decode("latin-1")
            vs, va, rs, rp = struct.unpack_from("<IIII", d, o + 8)
            ch = struct.unpack_from("<I", d, o + 36)[0]
            self.secs.append((name, va, vs, rs, rp, ch))

    def off(self, rva):
        for _n, va, vs, rs, rp, _c in self.secs:
            if va <= rva < va + max(vs, rs):
                return rp + rva - va if rva - va < rs else None
        return None

    def cstr(self, rva):
        o = self.off(rva)
        if o is None:
            return ""
        return self.d[o:self.d.index(b"\0", o)].decode("latin-1")

    def tables(self):
        """The import and export tables as text, and the file ranges they
        take up (left out of the string scan)."""
        out, spans = [], []
        rva, size = self.dirs[0]                      # exports
        if rva and size:
            o = self.off(rva)
            spans.append((o, o + size))
            name, _base, nfun, nnames, _af, an, _ao = struct.unpack_from(
                "<IIIIIII", self.d, o + 12)
            out.append("export name " + self.cstr(name))
            for i in range(nnames):
                out.append("export " + self.cstr(struct.unpack_from(
                    "<I", self.d, self.off(an) + 4 * i)[0]))
        rva, size = self.dirs[1]                      # imports
        if rva:
            o = self.off(rva)
            spans.append((o, o + size))
            while True:
                ilt, _t, _f, name, iat = struct.unpack_from("<IIIII", self.d, o)
                if not (ilt or name or iat):
                    break
                dll = self.cstr(name)
                no = self.off(name)
                spans.append((no, no + len(dll) + 1))
                t = self.off(ilt or iat)
                while True:
                    e = struct.unpack_from("<I", self.d, t)[0]
                    if not e:
                        break
                    if e & 0x80000000:
                        out.append("import %s #%d" % (dll, e & 0xFFFF))
                    else:
                        fn = self.cstr(e + 2)
                        ho = self.off(e)
                        spans.append((ho, ho + 2 + len(fn) + 1))
                        out.append("import %s %s" % (dll, fn))
                    t += 4
                o += 20
        for i in (12,):                               # the IAT itself
            rva, size = self.dirs[i]
            if rva and size:
                o = self.off(rva)
                spans.append((o, o + size))
        return out, spans

    def relocated(self):
        """File offsets of the four bytes of every address the loader fixes
        up (the base relocations): pointers, not text."""
        out = set()
        rva, size = self.dirs[5]
        if not rva:
            return out
        o, end = self.off(rva), self.off(rva) + size
        while o < end:
            page, blk = struct.unpack_from("<II", self.d, o)
            if blk < 8:
                break
            for i in range((blk - 8) // 2):
                e = struct.unpack_from("<H", self.d, o + 8 + 2 * i)[0]
                if e >> 12 == 3:
                    f = self.off(page + (e & 0xFFF))
                    if f is not None:
                        out.update(range(f, f + 4))
            o += blk
        return out

    def text(self):
        out, spans = self.tables()
        fixups = self.relocated()
        for name, va, vs, rs, rp, ch in self.secs:
            if ch & IMAGE_SCN_MEM_EXECUTE or name in (".reloc", ".rsrc"):
                continue
            blob = bytearray(self.d[rp:rp + rs])
            for f in fixups:
                if rp <= f < rp + rs:
                    blob[f - rp] = 1          # a pointer's byte: never text
            for lo, hi in spans:
                for f in range(max(lo, rp), min(hi, rp + rs)):
                    blob[f - rp] = 1
            for m in RUN.finditer(blob):
                a, s = m.start(), m.group()
                # a literal starts 4-byte aligned; what printable bytes come
                # before it are the end of other data (a float's high byte)
                cut = (-(va + a)) % 4
                a, s = a + cut, s[cut:]
                if wanted(s):
                    out.append(s.decode("latin-1"))
        return collections.Counter(out)


def wanted(s):
    """Text, not table bytes that happen to be printable."""
    t = s.strip()
    if len(t) < 5:
        return False
    letters = sum(c.isalpha() for c in t.decode("latin-1"))
    return letters >= 4 and letters * 2 >= len(t) - t.count(b" ")


def esc(s):
    return s.encode("unicode_escape").decode("ascii")


def unesc(s):
    return s.encode("ascii").decode("unicode_escape")


def load_known():
    """[(image, side, string, reason)] in file order."""
    rows, reason = [], ""
    p = os.path.join(REPO, KNOWN)
    if not os.path.exists(p):
        return rows
    for line in open(p, encoding="ascii"):
        line = line.rstrip("\n")
        if line.startswith("#"):
            reason = line
        elif line.strip():
            image, side, s = line.split("\t", 2)
            rows.append((image, side, unesc(s), reason))
    return rows


def differences(only=None):
    """{(image, side): Counter(strings)} for every image that is built."""
    out = {}
    for label, ref, ours in IMAGES:
        if only and label != only:
            continue
        rp, op = os.path.join(REPO, ref), os.path.join(REPO, ours)
        if not os.path.exists(rp) or not os.path.exists(op):
            print("datacheck.py: %s: %s missing, not checked"
                  % (label, rp if not os.path.exists(rp) else op), file=sys.stderr)
            continue
        a, b = Image(rp).text(), Image(op).text()
        out[(label, "retail")] = a - b
        out[(label, "ours")] = b - a
    return out


def compare(diff, known):
    """(new, fixed): differences not in the list, and list entries (as
    (image, side, string)) that no longer differ. An entry whose string is
    `*` allows every difference of its image and side."""
    allowed = collections.Counter((i, s, t) for i, s, t, _r in known if t != "*")
    every = {(i, s) for i, s, t, _r in known if t == "*"}
    have = collections.Counter()
    for (image, side), c in diff.items():
        if (image, side) in every:
            continue
        for t, n in c.items():
            have[(image, side, t)] += n
    new = have - allowed
    checked = {k[0] for k in diff}
    fixed = collections.Counter({k: n for k, n in (allowed - have).items()
                                 if k[0] in checked})
    return new, fixed


def report(diff, new, fixed, verbose=False):
    known_n = collections.Counter()
    for (image, side), c in diff.items():
        known_n[image] += sum(c.values())
    print("data check (text outside code and resources; progress/data-known.txt):")
    for label, _r, _o in IMAGES:
        if (label, "retail") not in diff:
            continue
        r = sum(diff[(label, "retail")].values())
        o = sum(diff[(label, "ours")].values())
        n = sum(v for k, v in new.items() if k[0] == label)
        print(f"  {label:<14} {r:>4} only in retail, {o:>4} only in ours"
              f"{f', {n} NEW' if n else ''}")
        if verbose:
            for side in ("retail", "ours"):
                for t in sorted(diff[(label, side)].elements()):
                    print(f"      {side:<6} {esc(t)[:110]}")
    for (image, side, t), n in sorted(new.items()):
        print(f"NEW: {image}: {'only in the retail image' if side == 'retail' else 'only in ours'}: "
              f"{esc(t)[:120]}" + (f" (x{n})" if n > 1 else ""))
    for (image, side, t), n in sorted(fixed.items()):
        print(f"no longer differs: {image} {side} {esc(t)[:100]}"
              + (f" (x{n})" if n > 1 else ""))


def write_known(known, fixed):
    """The list without the entries that no longer differ."""
    drop = collections.Counter(fixed)
    p = os.path.join(REPO, KNOWN)
    lines = open(p, encoding="ascii").read().split("\n")
    out = []
    for line in lines:
        if line and not line.startswith("#"):
            image, side, s = line.split("\t", 2)
            k = (image, side, unesc(s))
            if drop[k]:
                drop[k] -= 1
                continue
        out.append(line)
    # a reason left with no entries goes too
    keep = []
    for i, line in enumerate(out):
        if line.startswith("#") and not line.startswith("##"):
            rest = out[i + 1:]
            nxt = next((l for l in rest if l.strip()), "")
            if not nxt or nxt.startswith("# "):
                continue
        if line or (keep and keep[-1]):
            keep.append(line)
    open(p, "w", encoding="ascii").write("\n".join(keep).rstrip("\n") + "\n")


def main():
    os.chdir(REPO)
    IMAGES[:] = images()
    args = sys.argv[1:]
    cmd = args[0] if args else "check"
    if cmd == "show":
        diff = differences(args[1] if len(args) > 1 else None)
        new, fixed = compare(diff, load_known())
        report(diff, new, fixed, verbose=True)
        return 0
    if cmd == "baseline":
        only = None
        if len(args) > 1 and args[1].startswith("--image="):
            only = args.pop(1).split("=", 1)[1]
        if len(args) < 2:
            sys.exit(__doc__)
        new, _fixed = compare(differences(only), load_known())
        p = os.path.join(REPO, KNOWN)
        head = "" if os.path.exists(p) else (
            "## The text differences from the retail images that are known: "
            "tools/datacheck.py.\n"
            "## <image><TAB><retail|ours><TAB><string>; `# ...` is the reason "
            "for the entries after it.\n")
        with open(p, "a", encoding="ascii") as f:
            f.write(head + "\n# " + " ".join(args[1:]) + "\n")
            for (image, side, t), n in sorted(new.items()):
                for _ in range(n):
                    f.write(f"{image}\t{side}\t{esc(t)}\n")
        print(f"datacheck.py: {sum(new.values())} difference(s) recorded in {KNOWN}")
        return 0
    if cmd not in ("check", "update"):
        sys.exit(__doc__)
    known = load_known()
    diff = differences()
    new, fixed = compare(diff, known)
    report(diff, new, fixed)
    if cmd == "update" and fixed:
        write_known(known, fixed)
        print(f"datacheck.py: dropped {sum(fixed.values())} entr(y/ies) that no "
              f"longer differ from {KNOWN}")
    if new:
        print(f"FAIL: {sum(new.values())} new text difference(s) from the retail "
              f"images; fix them, or list them with their reason in {KNOWN}")
        return 1
    print("PASS: every text difference from the retail images is a known one")
    return 0


if __name__ == "__main__":
    sys.exit(main())
