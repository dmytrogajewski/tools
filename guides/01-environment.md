# 01 · The build environment

To reproduce Monolith's bytes you must compile with the compilers they used:
**Microsoft Visual C++ 6.0** -- several revisions of it, because Monolith did
not build every binary with the same one -- and, for the renderer `d3d.ren`,
**Intel C/C++ 4.0**. All of them are in the `toolchains/` submodule
(`git submodule update --init`), assembled from public copies of the
original media (see [`toolchains/README.md`](../toolchains/README.md)):

| directory | compiler | used for |
|---|---|---|
| `toolchains/msvc-6.00-9782/` | cl 12.00.8804 driver, back end 12.00.9782 (VC6 SP6) | the default; game DLLs, `NOLF.exe`, `ltmsg.dll`, `sres.dll`; and the linker, librarian and resource compiler for everything |
| `toolchains/msvc-6.00-8447/` | back end 12.00.8447 | `server.dll`, `lithtech.exe`, the lithshared libraries |
| `toolchains/msvc-6.00-8168/` | back end 12.00.8168 | the headers and libraries `icl` builds against; `CL_REV=8168` experiments |
| `toolchains/icl-4.0/` | Intel C/C++ 4.0 | `d3d.ren` |

`tools/richhdr.py` reads which revision built each retail image out of the
image itself; [`toolchains/COMPILERS.md`](../toolchains/COMPILERS.md) has the
answers and the evidence.

## Everything runs in the build container

The compilers are Win32 programs and run under Wine inside a Docker container
(Ubuntu 24.04, `docker/Dockerfile`), with the checkout mounted at `/repo` and
the container running as your user. The host needs Docker and nothing else --
no Wine, no Python.

```sh
make            # the whole build, in a throwaway container
make shell      # an interactive shell in the container, at /repo
```

Inside `make shell`, every build script and tool runs on its own (the Wine
prefix lives in `out/wineprefix`, created on first use). The rest of these
guides show commands as you type them there.

## Building parts of the tree

```sh
build/libs.sh ltguimgr          # one lithshared library (or: all)
build/build.sh cshell final     # one game DLL (cres | cshell | object | all)
build/engine.sh server          # all of one engine target's objects
build/engine.sh server verify   # ... then match every function against the retail image
build/engine.sh server-link     # link server.dll
```

`build/libs.sh` targets: `stdlith cryptmgr lith rezmgr regmgr mfcstub ztools
butemgr ltguimgr lithfontmgr gamespy all`; it writes the libraries to
`src/lithshared/out/`. `regmgr` also builds `RegMgr_Debug.lib`, the same
source with the `.dsp`'s Debug settings, which only `CShell.dll` links (the
Game of the Year `CShell.dll` has the Debug build's code). `build/engine.sh` with no arguments prints its targets;
its header lists the compiler and flags for each.

`src/engine/server/` is compiled into **both** `server.dll` (`/O1`) and
`lithtech.exe` (`/O2`). After changing a file there, rebuild both
(`build/engine.sh server` and `build/engine.sh lithtech`, or the two `*-one`
loops below), or `lithtech.exe` keeps scoring stale objects.

## Compiling one file at a time

While iterating on a single function you want a tight loop: recompile just
the one translation unit and check just its object. For the engine targets
`build/engine.sh` has that built in, with each file's own compiler and flags,
in about three seconds:

```sh
build/engine.sh server-one smgr.cpp PlaySound     # server.dll
build/engine.sh lithtech-one smgr.cpp PlaySound   # lithtech.exe (a name not in
                                                  #   src/engine/lithtech is
                                                  #   taken from src/engine/server)
build/engine.sh d3d-one d3d.cpp RenderDLLSetup    # d3d.ren
```

The optional last argument narrows the report to the lines naming that symbol.

For anything else, `build/env.sh` defines `run_cl` / `run_icl` / `run_lib` /
`run_link` / `run_rc`, the compilers under Wine. Setting `CL_REV` before
sourcing it selects the cl revision (`8168` or `8447`; unset is `msvc-6.00-9782`):

```sh
cd /repo/src/lithshared/ltguimgr
CL_REV=8447 source /repo/build/env.sh
rm -f Release/ltguibitmapctrl.obj
run_cl /nologo /MT /W3 /GX /O2 /D WIN32 /D NDEBUG /D _WINDOWS /D NO_PRAGMA_LIBS \
    /D _NOMFC /D _LITHTECH2 /I ..\\..\\LT2\\lithshared\\ltguimgr \
    /I ..\\..\\LT2\\lithshared\\incs /I ..\\..\\LT2\\lithshared\\mfcstub \
    /I ..\\..\\LT2\\lithshared\\lith /I ..\\..\\LT2\\sdk\\inc /I . \
    /c /FoRelease/ ltguibitmapctrl.cpp
```

The flags come from the original `.dsp` project files: **`/MT /W3 /GX /O2`**
is the Release recipe for the libraries and game DLLs, and matching it is
mandatory -- `/O2` vs `/O1`, `/MT` vs `/MD`, a missing `/D` all change
codegen. `/D NO_PRAGMA_LIBS` suppresses the `#pragma comment(lib, ...)`
directives the LT2 headers embed (they name Monolith's own build paths and
break linking). The `build_*` function for your library in `build/libs.sh`
has its exact include paths and defines; mirror them.

A failed compile under Wine can exit 0 and leave the previous object in
place, so a broken edit measures as "no change". The `*-one` loops delete the
old object first; do the same when compiling by hand.

## What "the same bytes" requires

- **Same compiler revision** -- the one the original binary was built with
  ([`toolchains/COMPILERS.md`](../toolchains/COMPILERS.md)); the build scripts
  pin it per target.
- **Same optimisation and runtime flags** -- per the `.dsp` files for the
  libraries and game DLLs; measured, per file, for the engine.
- **Same headers** -- the LT2-era headers in `src/LT2/`, which the include
  paths put first. Watch for local `.h` shadows that quote-include and
  override them.
- **Same source constructs** -- everything else is
  [03-reading-disassembly](03-reading-disassembly.md).

## Which toolchain built a given retail binary

`link.exe` stamps every PE with a "Rich" header: an obfuscated record of each
tool that contributed objects, as (product, build, object count). It is the
ground truth for "which compiler do we need", and it does not agree across
NOLF's binaries:

```sh
python3 tools/richhdr.py out/reference/NOLF.exe out/reference/server.dll \
    out/reference/lithtech.exe out/reference/ltmsg.dll
```

**Run it on our own output too, and mind which file you are reading.** Both
the retail launcher and ours are called `NOLF.exe`; ours is in
`src/engine/launcher/Release/`. Reading the rebuild and calling it the retail
file is an easy mistake and it inverts the conclusion.

| binary | cl builds present | its own C++ objects |
|---|---|---|
| `ltmsg.dll` | 8168 | 8168 ×2; it is resources around an empty `DllMain`, so its code is almost all CRT |
| `NOLF.exe` | 8168, 8569, 8797, 8799 | 8799 ×10 and 8797 ×10; the C rows (8797 ×138) are the CRT and 8569 ×42 is MFC |
| `server.dll` | 7291, 7299, 8168, 8447 | 8447 ×95 |
| `lithtech.exe` | 7291, 7299, 8168, 8447, 8799 | 8447 ×155 |
| `d3d.ren` | 7291, 7299, 8168, 8447 | 8168 ×8 and 8447 ×3 -- linked-in library code; the renderer itself was compiled by Intel C/C++, which leaves no Rich entry |

Our own builds with `msvc-6.00-9782` stamp 9782 -- which appears in none of the
retail images. `NOLF.exe` wants revisions nobody has (8797, 8799), yet the overwhelming majority of its functions match under the
default compiler anyway: the builds agree on almost everything, so **a near
match is far more likely to be a source shape you have not found than a
compiler difference**. Treat revision variance as the last explanation, never
the first, and claim it only with evidence -- typically, by rebuilding under
every held revision (`CL_REV=8168`, `CL_REV=8447`, unset) and showing none of
them matches.

Static libraries have an exact, per-object version of the same evidence: the
`@comp.id` symbol in every object, which `tools/compid.py` reads (see
[`toolchains/COMPILERS.md`](../toolchains/COMPILERS.md#a-second-exact-oracle-compid)).
