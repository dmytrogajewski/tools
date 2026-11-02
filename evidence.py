#!/usr/bin/env python3
"""The evidence a scoring run rests on, as one record.

A score is only as good as what it compared: which retail images, which
build of ours from which source, which tools and exclusions, which
compilers. `make progress` writes all of it to out/progress/evidence.json
after scoring, and `make progress-update` keeps a copy in
progress/evidence.json beside the baseline it records:

  source      the repository's revision (and how many files differ from
              it), and each submodule's
  reference   every target's retail image, SHA-256 (out/reference/ is
              checked against progress/reference.sha256 before scoring)
  config      SHA-256 of progress/targets.py and every exclusion, evidence
              and known-difference file in progress/
  build       per target: our image, its .map, and its link's .rsp and .cmd
              (the link command itself, as text)
  toolchains  every toolchains/<tree> a compile or link command of the
              build names: the digest toolchains/fetch.sh pins for it, and
              the tree's digest computed again; a mismatch fails the run
  result      SHA-256 of the run's functions.csv and summary.json

Every path in it is relative to the repository.

  evidence.py [out]          (default out/progress/evidence.json)
"""
import glob
import hashlib
import json
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import progress  # noqa: E402

REPO = progress.REPO


def sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def git(*args, cwd=REPO):
    try:
        return subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                              text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def tree_sha(d):
    """toolchains/fetch.sh's tree digest: sha256 over `sha256sum` of every
    file, sorted by path (C locale)."""
    lines = []
    for root, _dirs, files in os.walk(d):
        for f in files:
            p = os.path.join(root, f)
            lines.append((os.path.relpath(p, d), sha(p)))
    lines.sort(key=lambda x: x[0].encode())
    text = "".join("%s  ./%s\n" % (h, p) for p, h in lines)
    return hashlib.sha256(text.encode()).hexdigest()


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(progress.OUT, "evidence.json")
    os.chdir(REPO)
    ev = {"source": {}, "reference": {}, "config": {}, "build": {},
          "toolchains": {}, "result": {}}

    # (in a container the repository's .git may not be there -- talon is a
    # directory of lithtools: the host passes what it knows as
    # EVIDENCE_REVISION, EVIDENCE_MODIFIED and EVIDENCE_SUBMODULES)
    head = git("rev-parse", "HEAD") or os.environ.get("EVIDENCE_REVISION")
    dirty = git("status", "--porcelain", "--untracked-files=no")
    ev["source"]["revision"] = head
    if dirty is not None:
        ev["source"]["modified_files"] = len(dirty.splitlines()) if dirty else 0
    elif os.environ.get("EVIDENCE_MODIFIED", "").isdigit():
        ev["source"]["modified_files"] = int(os.environ["EVIDENCE_MODIFIED"])
    subs = git("submodule", "status") or \
        os.environ.get("EVIDENCE_SUBMODULES", "").replace(";", "\n")
    for line in subs.splitlines():
        parts = line.strip().lstrip("+-U").split()
        if len(parts) >= 2:
            ev["source"]["submodule " + parts[1]] = parts[0]

    for t in progress.TARGETS:
        p = t.get("shipped")
        if p and os.path.exists(p):
            ev["reference"][p] = sha(p)

    for p in sorted(glob.glob(os.path.join(progress.DATA, "*"))):
        if os.path.isfile(p) and not p.endswith(("evidence.json", "summary.json",
                                                  "identical.txt", "README.md")):
            ev["config"][p] = sha(p)

    used = set()
    tc_re = re.compile(r"toolchains[/\\]([A-Za-z0-9_.+-]+)")
    for t in progress.TARGETS:
        b = progress.built_image(t)
        if not b:
            continue
        rec = {"image": sha(b)}
        stem = os.path.splitext(b)[0]
        for ext in (".map",):
            if os.path.exists(stem + ext):
                rec["map"] = sha(stem + ext)
        for side in (".rsp", ".cmd"):
            q = b + side
            if os.path.exists(q):
                rec[side[1:]] = sha(q)
                if side == ".cmd":
                    rec["link_command"] = open(q, errors="replace").read().strip()
        ev["build"][t["label"]] = rec
        # the compilers: every command file in the build's directories
        d = os.path.dirname(b)
        for c in glob.glob(os.path.join(d, "**", "*.cmd"), recursive=True):
            used.update(tc_re.findall(open(c, errors="replace").read()))
    pinned = {}
    fetch = os.path.join("toolchains", "fetch.sh")
    if os.path.exists(fetch):
        for m in re.finditer(r"\[([A-Za-z0-9_.+-]+)\]=([0-9a-f]{64})",
                             open(fetch).read()):
            pinned[m.group(1)] = m.group(2)
    for name in sorted(used):
        rec = {"pinned": pinned.get(name)}
        if os.path.isdir(os.path.join("toolchains", name)):
            got = tree_sha(os.path.join("toolchains", name))
            rec["computed"] = got
            rec["matches"] = got == rec["pinned"]
        ev["toolchains"][name] = rec

    for f in ("functions.csv", "summary.json"):
        p = os.path.join(progress.OUT, f)
        if os.path.exists(p):
            ev["result"][p] = sha(p)

    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w") as f:
        json.dump(ev, f, indent=1, sort_keys=True)
        f.write("\n")
    bad = [n for n, r in ev["toolchains"].items() if r.get("matches") is False]
    print("evidence: %s (%d reference images, %d builds, %d toolchains%s)" % (
        out, len(ev["reference"]), len(ev["build"]), len(ev["toolchains"]),
        "; DIGEST MISMATCH: " + ", ".join(bad) if bad else
        ", their digests verified"))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
