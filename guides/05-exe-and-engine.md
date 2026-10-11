# 05 · Reconstructing executables and engine binaries

Libraries are the easy case: they ship as `.lib` files full of COFF symbols, so
`objdiff` can match by name and mask relocations. The engine targets —
`NOLF.exe`, `lithtech.exe`, `d3d.ren`, `server.dll` — are stripped release PE
files: **no symbols, and no `.reloc` section to speak of.** That changes the
workflow.

## What's different

- **No function names.** You work by address. `.text` is carved into
  functions with synthetic names (`sub_<addr>`); `build/reference.sh` writes
  that table for every image to `out/reference/<stem>-functions.csv`, and
  `tools/progress.py` gives each row a status in `out/progress/functions.csv`.
- **No relocations to mask on the shipped side.** Whole-file diffing is
  unreliable. Instead you verify *your* objects (which *do* have relocations)
  against the image with `exematch`: it masks your object's relocation bytes
  and scans the image's `.text` for the remaining position-independent bytes.
  A hit means that function is byte-identical in the shipped image.
- **CRT/MFC noise.** A release image is mostly library code you don't
  reconstruct. Only Monolith's own functions count. For `NOLF.exe` the app
  region is `0x401000–0x409ed2`; above it is the static MFC and CRT. Each
  image's region is `APP_REGION` in `tools/progress.py`, with the evidence for
  where it ends.

## The tools

All of these run inside `make shell` (or on a host with Python 3), after
`build/reference.sh` has put the retail images in `out/reference/`.

```sh
# annotated disassembly of a range (resolves IAT calls, inlines string/section refs)
tools/exedump.py out/reference/NOLF.exe 0x404fc0 0x4054b0

# verify: does the image contain our objects' functions, byte-for-byte?
tools/exematch.py out/reference/NOLF.exe src/engine/launcher/Release/*.obj
```

`exedump` is your `dumpfn` here — read a function, identify its imports and
string literals (which name what it does), and write the source. `exematch` is
your `objdiff` — it reports `MATCH <addr>` per function, or nothing for a miss.
(`exedump` needs `objdump` from binutils, which the build image does not have;
run it on a host that does.)

## The method

1. **Enumerate and map.** `tools/enumfuncs.py` collects function starts from
   several independent sources and reports byte coverage:

   ```sh
   tools/enumfuncs.py out/reference/server.dll --csv out/reference/server-functions.csv
   ```

   (`build/reference.sh` already does this for every image.) Use all the
   sources — none is sufficient alone. In server.dll only about 60% of the
   functions are direct `call` targets; nearly a third are reachable **only
   through vtables or function tables**, and the rest through callbacks,
   initializer pairs or nothing at all. (Padding-carving works on
   lithtech.exe but *not* on server.dll or d3d.ren, which have almost no
   inter-function padding.) Check the reported coverage: well under ~99% of
   `.text` means starts are still missing.

   Then build a table of contents: for each function, the strings and imports
   it references (grep the `exedump` output). Those references tell you what
   each function is and which ones group into a source file — and in the
   engine binaries, embedded `Class::Method` strings name many outright
   (`tools/tagmap.py`; see [06](06-worked-example-engine.md)).

2. **Seed from era source where it exists.** Monolith reused code across
   titles. `NOLF.exe`'s DIB/palette code is `libs/dibmgr` in the NOLF2 source
   release; its `RezFind`/`Utils` are in the NOLF2 launcher. Back-port these —
   but **trim to the shipped era**: the shipped code predates NOLF2 by years,
   so remove later-added functions and overloads (dibmgr's post-1998 methods,
   the Win2K PSAPI path in `ExistProcess`, and so on) until the function set
   matches. A seeded file often matches 80%+ on the first compile.

3. **Disassemble-and-match the rest.** Everything with no era source is read
   straight from `exedump` output using
   [03-reading-disassembly](03-reading-disassembly.md).

4. **Resources are separate.** An exe or dll also carries dialogs, strings,
   bitmaps, icons. Reconstruct them as an `.rc` and verify with `rsrcdiff`
   (payload by payload, layout-independent) — see how `src/engine/launcher/`
   and `src/engine/ltmsg/` do it. `ltmsg.dll` is *only* resources: its `.text`
   is the bare CRT around an empty `DllMain`.

5. **Structural tells for exes.** `NOLF.exe` is a Win32 app over *static*
   MFC — the dialogs are raw `DLGPROC`s driven by `DialogBoxParamA`, not MFC
   message maps. Recognizing the framework early saves you modelling the
   wrong thing.

## Compiling and verifying

**`server.dll` is built `/O1` (minimize size), not `/O2`** — proven by
byte-matching it; [06-worked-example-engine](06-worked-example-engine.md)
shows how that was found and what the tells look like. The other targets have
their own compilers and flags (lithtech.exe cl 8447 `/O2`, d3d.ren Intel
C/C++ 4.0 with per-file flag groups); the header of `build/engine.sh` lists
them, and `toolchains/COMPILERS.md` gives the evidence.

`build/engine.sh` builds each target and, with `verify`, runs `exematch` on
every object:

```sh
make shell                                # then, inside the container:
build/engine.sh launcher verify           # compile, link NOLF.exe, match
build/engine.sh server verify
```

While iterating on one function, recompile only its file and match only its
object — a few seconds per round:

```sh
build/engine.sh server-one servermsg.cpp WriteToMessageByte
build/engine.sh lithtech-one smgr.cpp PlaySound
build/engine.sh d3d-one d3d.cpp RenderDLLSetup
```

The optional last argument narrows the report to the lines naming that
symbol. `src/engine/server/*.cpp` is compiled into **both** server.dll and
lithtech.exe, so after changing one, check both.

## The whole-image link

`exematch` verifies functions individually against the shipped bytes; that
is the real proof of faithfulness. A *complete* byte-identical image
additionally needs every function reconstructed and the linker settings,
order and data matched. Until then, the per-function match rate — the table
in the top-level `README.md`, re-measured by `make progress` — is the measure
of progress.

## Running the thing — nears have to execute

A byte-identical function needs no runtime test: it *is* the shipped code. A
**near** is the opposite — instruction-equivalent but not proven, and
therefore not shown to be correct. The whole links close that gap: every
engine image is linked from our objects alone (`build/engine.sh
server-link`, `lithtech-link`, `d3d-link`), `make` assembles them into
`out/nolf-legacy-release/`, and `make test` plays levels on them. Every near we have written
runs there, beside every identical function. `server-link test` and
`d3d-link test` load a single linked image under Wine on its own.

The lesson that carried over from running nears: **a near can be the wrong
function.** Pairing by shape once matched a thiscall `AllocItems` (`ret 8`)
against a cdecl allocator (plain `ret`), and the game crashed on load; of the
functions a return-style check then flagged, two were real bugs in the
reconstruction. Compare the return instructions before believing a pairing.

## The engine binaries

`lithtech.exe`, `d3d.ren` and `server.dll` are the bulk of the work — about
1.4 MB of `.text` between them. How far each has got is the README's
progress table.

`server.dll` was the model for the others: it exports only
`CreateServer`/`DeleteServer`, the LT2 SDK headers give type information the
launcher work never had, and it carries embedded `Class::Method` strings that
name functions for free. Its recovered source is also lithtech.exe's: the
client engine compiles the same `src/engine/server/` files a second time, so
work there pays twice.

A caution learned there: the SDK headers in `src/LT2/sdk/inc/` are
**NOLF2-era** while these binaries are NOLF1. Signatures are mostly stable;
**vtable order is not**. Verify every slot's argument count from its `ret N`
before trusting the header's ordering.
