# 02 · The verification loop

The whole method rests on a strict oracle: tools that tell you, per function,
whether your machine code equals the retail machine code. You never guess
whether you are right -- you diff.

## The tools

All live in `tools/` and are plain Python 3 (the disassemblers also need
`objdump` from binutils, which the build container does not have: run those
on a host with binutils, or install it). [`tools/README.md`](../tools/README.md)
describes every tool; these are the core of the loop.

| Tool | What it does | Use when |
|---|---|---|
| `objdiff.py` | Per-function byte compare of two `.obj`/`.lib`, by symbol, relocations masked | Libraries (they keep their COFF symbols) |
| `libextract.py` | Extract the members of a `.lib` | Getting at a retail library's objects |
| `dumpfn.py` | Disassemble one function from a `.obj` in isolation | Inspecting your output, or a retail library object |
| `exedump.py` | Annotated disassembly of a PE `.text` range (imports and strings resolved) | Reading a retail exe/dll with no symbols |
| `exematch.py` | Search a retail image for each function of our objects, relocations masked | Verifying engine reconstructions |
| `fndiff.py` | One-shot retail-vs-ours function diff: bytes, mnemonics, best register renaming, fingerprint hints | The engine iteration loop |
| `bindiff.py` | Segment-level code compare of two whole PE files (relocation-normalised) | Whole-DLL sanity (`ltmsg.dll`, game DLLs) |
| `rsrcdiff.py` | Compare PE resource trees payload by payload | Resource targets (`ltmsg.dll`, the launcher) |
| `progress.py` | Per-function scoreboard across all targets | Tracking; picking work |

The fast engine loop is these two commands, about three seconds a cycle (in
`make shell`):

```sh
build/engine.sh server-one smgr.cpp PlaySound      # recompile ONE file, match one symbol
python3 tools/fndiff.py 0x10034037 src/engine/server/Release/smgr.obj PlaySound
```

One thing about the second command bites everybody once: its options need an
equals sign -- `--end=0x100044b4`, `--image=out/reference/lithtech.exe`,
`--csv=...`; written with a space it prints the usage text instead. Its
default image is `out/reference/server.dll`.

`fndiff.py` reads the retail function's size from the image's function table
(`out/reference/<stem>-functions.csv`, written by `build/reference.sh`),
checks byte identity with relocations masked, shows the mnemonic diff, then
retries it under every renaming of `ebx`/`esi`/`edi` (separating
register-allocation noise from structural differences), and prints hints
matched against the codegen fingerprints catalogued in
[09-codegen-patterns](09-codegen-patterns.md). Its byte-level view also
catches what opcode-masked diffs cannot: two same-shaped instructions with
*swapped field offsets* look identical once immediates are masked -- that
exact blind spot hid a crossed pair of struct fields in `CNetMgr::Update`.

The whole-target checks are the same tools run over every object:

```sh
build/engine.sh server verify          # exematch over every server.dll object
build/engine.sh launcher verify        # link NOLF.exe, match every launcher function
build/engine.sh ltmsg verify           # bindiff + rsrcdiff against the retail ltmsg.dll
make progress                          # the scoreboard, held to the recorded baseline
```

`exematch.py`'s own totals are not the ones to quote: it scores each of our
functions independently, so two of ours with identical code both "match" the
same retail address. `tools/progress.py` gives every retail function to at
most one of ours; its numbers are the authoritative ones.

## Why relocations are masked

A `call` to another function, or a `mov` of a global's address, encodes an
address the linker fills in -- it differs between our build and theirs for
reasons that have nothing to do with correctness. So the diff tools blank the
4 bytes each relocation covers before comparing. Two functions that match
after masking are the same code; the linker will place the addresses.

**This is also a blind spot:** a `call` to the *wrong* function of the same
shape still masks to a match, and so does a load of the wrong float constant
(`tools/fpconst.py` checks those). `objdiff`/`exematch` cannot catch a
miswired call -- `tools/callcheck.py` can, and so can *linking* (an
unresolved external) or running the game. After a library verifies clean,
link it into what uses it as a final check.

## The library loop (symbols available)

The retail lithshared libraries come from Monolith's *NOLF Source Code
v1.003* release, which you supply yourself (see
[`progress/README.md`](../progress/README.md#the-libraries-nolf_source)).
Start the shell with it, and it is mounted read-only with `$NOLF_SOURCE`
pointing at it:

```sh
make shell NOLF_SOURCE=<release>                          # on the host
LIBS=$NOLF_SOURCE/LT2/lithshared/libs/Release             # in the shell
```

The commands below unpack library members into `out/shipped/`, a scratch
directory.

1. **Dump the retail function** to read its codegen:
   ```sh
   python3 tools/libextract.py $LIBS/StdLith.lib out/shipped/stdlith
   python3 tools/dumpfn.py out/shipped/stdlith/dynarray.obj "?SomeFunc@"
   ```

2. **Write the C++** in `src/lithshared/<lib>/`.

3. **Compile** just that file (see [01-environment](01-environment.md)), or
   the whole library with `build/libs.sh <lib>`.

4. **Diff**, the whole library:
   ```sh
   python3 tools/objdiff.py src/lithshared/out/StdLith.lib $LIBS/StdLith.lib
   ```
   or one object while iterating:
   ```sh
   python3 tools/libextract.py $LIBS/ltguimgr_LT2.lib out/shipped/ltguimgr
   python3 tools/objdiff.py src/lithshared/ltguimgr/Release/ltguibitmapctrl.obj \
       out/shipped/ltguimgr/ltguibitmapctrl.obj
   ```
   `tools/libfndiff.py` is `fndiff`'s report for one library symbol
   (`python3 tools/libfndiff.py StdLith <symbol>`, which finds the retail library
   through `$NOLF_SOURCE`).

5. Read the `differing` list. For each, `dumpfn` both sides and compare.
   Adjust the source. Repeat until `identical : N/N`.

## Reading a diff

`objdiff` prints, per common function, `identical` or `differing (X vs Y
bytes)`. When sizes match but bytes differ, it is usually register allocation
or instruction scheduling -- a small source change (statement order, a temp
variable, a `>>` vs `/`) flips it. When sizes differ, you have a structural
mismatch (an extra branch, a wrong loop form, a missing or extra statement).

A useful trick for large functions -- normalise away register names and stack
offsets so only *structural* differences remain:

```sh
norm() { sed -E 's/%e[a-ds][ipx]/%R/g; s/0x[0-9a-f]+\(%esp[^)]*\)/SLOT/g'; }
diff <(python3 tools/dumpfn.py ours.obj Sym | norm) \
     <(python3 tools/dumpfn.py theirs.obj Sym | norm)
```

If the normalised diff is empty, all that is left is register and slot
assignment. If it is not, there is a real source difference to find.

Frame size is a free checksum along the way: add up the locals your source
declares and compare with the retail `sub esp,N`. A shortfall of twelve bytes
is almost always a vector operator's return temporary you wrote out by hand.

## When to stop at "near"

Leave a function `near` only when the normalised diff is empty or reduces to
one of the [known near classes](09-codegen-patterns.md#known-near-classes) --
tail merge, cross-jump host, shared-exit host, callee-saved rotation,
shrink-wrapping, FPU stack bookkeeping or term ordering. Note which class, in
a comment next to the function.

Before you do, two cheap checks that have both turned a "near" back into a
match: rebuild under the other compiler revisions we hold
(`CL_REV=8168 build/engine.sh server-one ...`, `CL_REV=8447`, and the
`msvc600` default), and confirm every call in the function pushes exactly
what the retail call pushes. Anything else is a real difference -- keep
going, and add what you learn to
[09-codegen-patterns](09-codegen-patterns.md).

## Ghidra as a reading aid (optional)

For a large or unfamiliar function, a decompiled C draft saves real reading
time. `tools/ghidra/decomp.sh` batch-decompiles a binary's app region:

```sh
tools/ghidra/decomp.sh out/reference/NOLF.exe 0x401000 0x409ed2 out/ghidra/nolf-app.c
```

It runs on the host, not in the build container: it needs
`ghidra.analyzeHeadless` on `PATH` (the Ghidra snap provides it) and about
2 GB of free RAM (the JVM is killed without it). Keep the output under `out/`
-- it is derived from the retail binary, and takes a few minutes to
regenerate.

**It is a reading aid, never the source.** The decompiler normalises away
precisely the things byte-matching depends on:

| the bytes distinguish | Ghidra prints both as |
|---|---|
| `while (*p)` vs `if (*p) do {…} while (*p)` | `while (*p)` |
| `HIWORD(w)==n` vs `(w>>16)==n` | `(w >> 16) == n` |
| `CString cs = x;` vs `CString cs; cs = x;` | `cs = x` |
| declaration order (which local takes a dead register) | any order |
| `?:` vs `if/else` returning two calls | a `?:` |

What it *is* good for: control flow at a glance, struct field offsets,
argument counts, and spotting an argument you misread -- it correctly typed
`PaintSplash`'s second parameter as `SelectPalette`'s `bForceBackground`
flag.

Three uses beyond a first draft, all of which have turned a near match into
an exact one:

- **Its function-ID database names CRT calls.** It identified 0x40b3b0 as
  `_mbscpy`, which settled a "register tie-break" that was really a source
  difference: `strcpy` is an intrinsic and expands inline, `_mbscpy` is not
  and calls. `Save3DSoundDlg` had been 18 bytes over for that reason alone.
- **Its `goto LAB_...` marks a genuinely shared block.** Where the retail
  code reaches one body from two places, writing the bodies out twice and
  hoping the compiler merges them is not the same code -- VC6 merges one pair
  and not the next. `CustomizeDlgProc` was 56 bytes over until the gotos went
  in.
- **Its if/else nesting shows branch polarity**, which is source, not
  codegen: `if (!p) { a } else { b }` and `if (p) { b } else { a }` differ.
  That was worth 217 bytes in `InitGameDlg`, 32 in `CustomizeDlgProc`, and
  re-ordered every call site in `SetDefaultModes`.

**Seed the function starts.** Ghidra misses functions reached only through a
`push imm32` handed to the API -- DLGPROCs, WNDPROCs -- and silently folds
them into whatever precedes them. Pass a file of function starts as the fifth
argument (the first column of `out/reference/<stem>-functions.csv`) and it
creates a function at each, splitting any over-extended one. On `NOLF.exe`
that is the difference between having and not having the preview and display
dialog procs.

**It reads our objects too.** `analyzeHeadless` imports a COFF `.obj` and
keeps the COMDAT symbol names, so a near match can be checked semantically:
decompile the retail function and our own object, and if the C differs, the
diagnosis of "compiler-revision variance" is wrong and there is still a
source fix to find.

Read the draft, then derive the real source from `tools/exedump.py` as usual.
