#!/usr/bin/env python3
"""Split a d3d translation unit in two so each half can carry its own flag.

d3d.ren is a MIXED build and our file boundaries are invented, so a file that
holds functions from two kinds of shipped TU loses whichever group the flag
does not suit.  build/engine.sh already compiles a measured list at /Os and
the rest at /O2; this moves individual FUNCTIONS across that line by making a
companion file.

The companion is a full copy of the original with the other function
definitions deleted -- not a hand-cut preamble.  Cutting a preamble by hand
was tried and it dropped declarations the moved bodies needed
(`identifier "g_100893dc" is undefined`), because in these files the extern
and class declarations are interleaved with the definitions rather than
gathered at the top.  Keeping every line that is not a function body keeps
every declaration by construction.

A definition is a top-level `{` ... `}` block whose header contains `(` and
whose close is a `}` in column 0.  Class and struct bodies end `};` and their
headers have no `(`, so they are never touched and stay in both halves.

Usage:
  d3dsplit.py <file.cpp> <symbol>...      -> writes <file>_os.cpp, edits <file>
"""
import os
import re
import sys


def blocks(lines):
    """Yield (start, end, header) for every top-level brace block."""
    i = 0
    while i < len(lines):
        if lines[i].startswith("}"):
            i += 1
            continue
        # A definition's header may span several lines; it opens at the first
        # line ending in `{` before any `;` at depth zero.
        if re.match(r"^[A-Za-z_~]", lines[i]):
            j, hdr = i, []
            while j < len(lines):
                hdr.append(lines[j])
                # Every declaration in these files carries a trailing `//`
                # comment giving its shipped address, so the terminator has
                # to be looked for with that comment removed -- testing the
                # raw line ran `struct C10091df0;  // opaque` on into the
                # next function and deleted the declaration with it.
                s = lines[j].split("//")[0].rstrip()
                if s.endswith(";"):
                    break
                if s.endswith("{"):
                    k = j + 1
                    depth = 1
                    while k < len(lines) and depth:
                        c = lines[k].split("//")[0]
                        depth += c.count("{") - c.count("}")
                        k += 1
                    yield i, k, "".join(hdr)
                    i = k - 1
                    break
                j += 1
            else:
                break
        i += 1


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    opts = dict(a.split("=", 1) for a in sys.argv[1:] if a.startswith("--"))
    if len(args) < 2:
        sys.exit(__doc__)
    path, keep = args[0], set(args[1:])
    lines = open(path).read().splitlines(keepends=True)

    found, cut_from_orig, cut_from_copy = set(), set(), set()
    for a, b, hdr in blocks(lines):
        if "(" not in hdr:
            continue                        # not a function definition
        mine = {s for s in keep if s in hdr}
        if mine:
            found |= mine
            cut_from_orig.update(range(a, b))
        else:
            cut_from_copy.update(range(a, b))

    missing = keep - found
    if missing:
        sys.exit(f"not found in {path}: {' '.join(sorted(missing))}")

    stem, ext = os.path.splitext(path)
    out = opts.get("--into") or f"{stem}_os{ext}"
    # --into appends to an EXISTING companion instead of making a new one.
    # A companion produced by this tool keeps every line that is not a
    # function body, so it already carries every declaration the original
    # had; moving another body into it therefore needs nothing but the body.
    if opts.get("--into"):
        with open(out, "a") as fh:
            fh.write("\n\n" + "".join(
                l for i, l in enumerate(lines) if i in cut_from_orig))
        with open(path, "w") as fh:
            fh.writelines(l for i, l in enumerate(lines)
                          if i not in cut_from_orig)
        print(f"{out}: appended {len(keep)}; {path}: removed "
              f"{len(cut_from_orig)} lines")
        return
    banner = (
        f"// Split out of {os.path.basename(path)}: these functions need the\n"
        f"// OTHER optimisation flag from the rest of that file.  d3d.ren is a\n"
        f"// mixed build and our file boundaries are invented; see\n"
        f"// tools/d3dsplit.py.  Everything that is not a function body is\n"
        f"// carried over verbatim so no declaration is lost.\n"
        f"// ********************************************************* //\n")
    with open(out, "w") as fh:
        fh.write(banner)
        fh.writelines(l for i, l in enumerate(lines) if i not in cut_from_copy)
    with open(path, "w") as fh:
        fh.writelines(l for i, l in enumerate(lines) if i not in cut_from_orig)
    print(f"{out}: kept {len(keep)}; {path}: removed {len(cut_from_orig)} lines")


if __name__ == "__main__":
    main()
