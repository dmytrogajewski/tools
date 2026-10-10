# tools/

The helpers the build (`build/*.sh`), `make test` and `make progress` run.
Plain Python 3 with no dependencies, plus one shell script. You do not
normally run them by hand, except the scoring tools while working on a
function.

| Tool | Used by | What it does |
|---|---|---|
| `discextract.py` | `build/all.sh` steps 1, 2, 2b, 8 | Takes what the build needs off the disc images: `resources` (game data, with the retail game DLLs inside the `.rez` archives zeroed), `thirdparty` (Miles and Bink, the only binaries not built from source), `launcherres` (the launcher's bitmap, palette, music, icon and cursors, read out of the retail `NOLF.exe`'s resources), `audit` (lists every PE file in the game directory with its origin, and fails on any it cannot account for), and `reference` (the retail `lithtech.exe`, `server.dll`, `d3d.ren` and `NOLF.exe`, for scoring only: `out/reference/`). |
| `isoextract.py` | `discextract.py` | Minimal ISO 9660 / Joliet reader. Also a CLI: `isoextract.py list <iso>`. |
| `rezextract.py` | `discextract.py` | Reader for Monolith's REZ archives. Also a CLI: `rezextract.py list <rez>`. |
| `dsp2files.py` | `build/build.sh` | Lists a VC6 `.dsp` project's source files for one configuration, so the game DLLs compile exactly the files the original project did. |
| `mkimplib.py` | `build/engine.sh` | Writes a COFF import library for a few named imports (Miles' decorated names, `DirectDrawCreateEx`). |
| `mkaliasobj.py` | `build/engine.sh` | Turns `src/engine/*/link/aliases.rsp` into an object of weak externals: one function or global reached under two names. |
| `mkvtabobj.py` | `build/engine.sh` | Turns `src/engine/lithtech/link/vtables.txt` into an object holding full-length vtables for classes the source only partly declares. |
| `mkviewobj.py` | `build/engine.sh` | Turns `src/engine/d3d/link/d3dviews.txt` into an object laying out `d3d.ren`'s globals as the original did. |
| `playtest.sh` | `make test` | Runs a game directory under Xvfb in the build container, optionally driving it with an input script, and reports ALIVE / EXITED / CRASHED (plus `wine.log` and screenshots in `out/play/<name>/`). Usage in its header. |
| `playtest-basic.txt` | `make test` | The input script: skip the opening cinematic, look, move, jump, duck, fire, switch weapons, open the menu, quicksave and quickload. |
| `progress.py` | `make progress` | The scoreboard: enumerates every function of the retail images and libraries and classifies each against our build (`identical`, `near`, `small`, `attempted`, `todo`), writing `out/progress/functions.csv` and `summary.json`. `--nears` lists the near matches closest first. See `progress/README.md`. |
| `ratchet.py` | `make progress` | Holds a scoring run to the baseline in `progress/`: `check` fails if a recorded identical function stopped matching, `update` records the run and rewrites `README.md`'s table, `readme` only the table. |
| `enumfuncs.py` | `progress.py` | Enumerates the functions of a symbol-less PE image (call targets, data pointers, callbacks, initializer pairs, exports, code after a `ret`). Also a CLI: `enumfuncs.py <image> [--csv out.csv]`. |
| `exematch.py` | `progress.py` | Searches an image's `.text` for each function of our objects, with relocation bytes masked. Also a CLI: `exematch.py <image> <obj\|lib>...` prints `MATCH 0x<addr>` per function -- the quick check while working on one; `progress.py`'s totals are the ones to quote, since it gives each retail function to one of ours only. |
| `objdiff.py` | `progress.py` | Compares two COFF objects or `.lib`s symbol by symbol, relocations masked. Also a CLI: `objdiff.py <ours> <theirs>`. |
| `libextract.py` | `progress.py`, `objdiff.py` | Extracts the members of a COFF `.lib` archive. |
| `d3dcheck.py` | `progress.py` | `d3d.ren` specifics: the Intel compiler's multi-byte padding, and the addresses of `d3d.ren` functions whose names do not carry one. Also a CLI that checks each `src/engine/d3d` function at the address its name gives. |
| `exedump.py` | `progress.py` (its PE reader) | Annotated disassembly of a range of a retail image (imports, strings and data references resolved): `exedump.py out/reference/server.dll 0x10034037 0x10034120`. Needs `objdump` (binutils), which the build image does not have. |
