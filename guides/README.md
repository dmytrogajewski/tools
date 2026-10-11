# Reconstruction guides

How to take one function from a retail NOLF binary and write C++ that the
original toolchain compiles to **byte-identical machine code**.

This is not a rewrite or a port. The goal is preservation: source that
reproduces the exact bytes Monolith shipped, verified function by function.
How far that has got is the table in the top-level [README](../README.md#progress);
`make progress` re-measures it (see [`progress/README.md`](../progress/README.md)).

## The loop

Every function follows the same cycle:

```
   pick a target ─▶ disassemble it ─▶ read the codegen back into C++
        ▲                                        │
        │                                        ▼
   verify (diff) ◀── compile it ◀────────── write the source
        │
        └─▶ identical? next function.   near? adjust source, loop.
```

You are done with a function when the tools report it **identical** (machine
code equal, relocation targets masked). A **near** match --
instruction-equivalent but a few bytes off -- is acceptable only when the
remaining difference belongs to one of the
[known near classes](09-codegen-patterns.md#known-near-classes); document it
in a comment beside the function and move on. Coming back to close one later
is its own job, with its own method -- see
[10-upgrading-nears](10-upgrading-nears.md).

There is a **third standard**, for one specific kind of work: a function of
several kilobytes, where byte identity is out of reach but the decode is the
valuable part. Those are reconstructed to be *structurally* right and proved
with the call-sequence check rather than the byte oracle -- see
[11-large-functions](11-large-functions.md). It relaxes codegen exactness and
nothing else: guessing is still forbidden, and an undecoded region is still
declared rather than filled in.

Three companion documents sit outside this directory and are worth reading
once: [`BUILDING.md`](../BUILDING.md) explains the build and how to run parts
of it, [`tools/README.md`](../tools/README.md) what each tool does and when
you reach for it, and [`toolchains/COMPILERS.md`](../toolchains/COMPILERS.md)
which compiler revision built which binary.

## Start here

1. **[01-environment](01-environment.md)** -- the compilers under Wine in the
   build container, and how to compile one file at a time.
2. **[02-the-loop](02-the-loop.md)** -- the verification tools (objdiff,
   dumpfn, exedump, exematch, fndiff, bindiff, rsrcdiff) and how to drive the
   compile-diff cycle.
3. **[03-reading-disassembly](03-reading-disassembly.md)** -- the reference:
   how VC6 codegen idioms map back to C++ source constructs. The heart of the
   work.
4. **[04-worked-example](04-worked-example.md)** -- one **library** function
   start to finish, every command shown (symbols available).
5. **[05-exe-and-engine](05-exe-and-engine.md)** -- the harder case: binaries
   with no symbols and no relocations (the engine `.exe`/`.dll`/`.ren`
   targets).
6. **[06-worked-example-engine](06-worked-example-engine.md)** -- one
   **engine** function start to finish: naming it from embedded strings,
   modelling the internal error macro and vtables, discovering the `/O1`
   build, and cashing in the rest of its cluster.
7. **[07-vtables](07-vtables.md)** -- naming *anonymous* functions: find the
   class's vtable in `.rdata` (`tools/vtabmap.py`), anchor it against the SDK
   header's declaration order, and read the unknown slots' names and
   signatures straight out of the header.
8. **[08-worked-example-playsound](08-worked-example-playsound.md)** --
   chasing a 494-byte function to byte identity: second-base `this`
   adjustment, dead-parameter-slot locals, the mnemonic diff, and reading
   register allocation and block layout back into source shape.
9. **[09-codegen-patterns](09-codegen-patterns.md)** -- the case law: every
   proven asm-to-source fingerprint, organised by what you see in the
   disassembly, plus the near classes not worth chasing and the per-binary
   compiler map. Read it before writing a function and again when one is
   stuck a few bytes off.
10. **[10-upgrading-nears](10-upgrading-nears.md)** -- taking a function that
    is *nearly* right and closing the gap: triaging the worklist, telling a
    real source fix from a missing function from an irreducible one, the
    fixes in the order they pay, and how to write a near up so nobody repeats
    it.
11. **[11-large-functions](11-large-functions.md)** -- the third standard:
    reconstructing a function of several kilobytes to be *structurally* right
    and proving it with the call-sequence check (`tools/callseq.py`).
12. **[12-o2-shape](12-o2-shape.md)** -- functions already byte-identical in
    `server.dll` (`/O1`) but not in `lithtech.exe` (`/O2`): the source is
    proven, only its `/O2` shape is wrong (`tools/o2gap.py`).
13. **[13-cluster-recon](13-cluster-recon.md)** -- the procedure for
    reconstructing a whole untouched cluster of functions from scratch.

## Picking a target

`tools/progress.py` scores every function of every target and writes
`out/progress/functions.csv`: one row per retail function, with its size and
status (`identical`, `near`, `small`, `attempted`, `todo`, `crt`; see
[`progress/README.md`](../progress/README.md) for what each means). Run
`make progress` (or `python3 tools/progress.py` in `make shell`) first, after
a `make`.

**What counts as a target.** Every image statically links the C runtime (and
MFC, in `NOLF.exe`). That code is real, but the toolchain supplies it -- it is
not something to reconstruct. Functions outside Monolith's own `APP_REGION`
(see `tools/progress.py`) are recorded as `crt` and left out of the counts.

To find work:

```sh
# untouched functions of one target, largest first
python3 tools/progress.py --todo=server.dll

# smallest untouched functions in a target (easiest first)
grep ',todo$' out/progress/functions.csv | grep '^lithtech.exe,' | sort -t, -k3 -n | head

# near matches, closest to identical first
python3 tools/progress.py --nears=server.dll
```

When you have moved the numbers, `make progress-update` records them (and
rewrites the README table); `make progress` fails if a function that was
recorded as identical stops matching, so run it before you hand work in.

Start small, and prefer a target that is the **first of a family**: the first
function in a cluster is expensive (you are modelling macros, object layouts
and flags), and the rest of the cluster then costs almost nothing. A 30-byte
leaf teaches the loop; save the 2000-byte register-pressured monster for
later.

The flags decide everything before you start:

| target | compiler | optimisation |
|---|---|---|
| lithshared libraries | cl 8447 | `/O2` |
| game DLLs (`CShell.dll`, `Object.lto`, `CRes.dll`) | cl (default `msvc-6.00-9782`) | `/O2` |
| `server.dll` | cl 8447 | `/O1` (one file `/Os`) |
| `lithtech.exe` | cl 8447 | `/O2` (a few files with their own flags) |
| `d3d.ren` | Intel C/C++ 4.0 | per-file groups (`/O2`, `/Os`, `/Qip`, ...) |
| `NOLF.exe`, `ltmsg.dll`, `sres.dll` | cl (default `msvc-6.00-9782`) | `/O2` |

The build scripts already use the right compiler and flags for every file
(`build/engine.sh`'s header lists them); the evidence for each choice is in
[`toolchains/COMPILERS.md`](../toolchains/COMPILERS.md). Check it before
diagnosing a near.

## Never change what the retail code does

The goal of this work is the retail game, bugs included: a function that
behaves differently from the retail one is wrong here even when the retail one
is the buggy one. When you find a bug, reproduce it and add it to
`RETAIL_BUGS.md`. A fix goes behind `#ifdef NOLFSRC_MODERN`, with the retail
code kept unchanged in the `#else` (see `BUILDING.md`, "Variant builds"), so
the release build -- the only one that is scored -- still matches.

## Ground truth is `out/reference/`

The retail binaries are extracted from disc 2 into `out/reference/` by
`build/reference.sh` (`make progress` does it too), alongside a function
table per image (`<image-stem>-functions.csv`). Every check and every
hand-run tool reads them from there -- `build/engine.sh ... verify`, the
`*-one` loops, `tools/progress.py`, and the defaults of `exedump.py`,
`fndiff.py`, `nearscan.py` and the rest. Never point a tool at one of our own
builds (`src/engine/*/Release/*.dll|.exe|.ren`, `out/nolf-legacy-release/`): it will read our
code back and report a match that proves nothing.

```sh
python3 tools/exedump.py out/reference/server.dll 0x100042db 0x100044b4
python3 tools/fndiff.py 0x100042db src/engine/server/Release/worldsim.obj \
    '?ws_PushBoxOutOfContacts@@YAXXZ' --image=out/reference/server.dll
```
