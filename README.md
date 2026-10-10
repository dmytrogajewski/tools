# tools/

The helpers the build (`build/*.sh`) and `make test` run. Plain Python 3
with no dependencies, plus one shell script. You do not normally run them by
hand.

| Tool | Used by | What it does |
|---|---|---|
| `discextract.py` | `build/all.sh` steps 1, 2, 2b, 8 | Takes what the build needs off the disc images: `resources` (game data, with the retail game DLLs inside the `.rez` archives zeroed), `thirdparty` (Miles and Bink, the only binaries not built from source), `launcherres` (the launcher's bitmap, palette, music, icon and cursors, read out of the retail `NOLF.exe`'s resources), and `audit` (lists every PE file in the game directory with its origin, and fails on any it cannot account for). |
| `isoextract.py` | `discextract.py` | Minimal ISO 9660 / Joliet reader. Also a CLI: `isoextract.py list <iso>`. |
| `rezextract.py` | `discextract.py` | Reader for Monolith's REZ archives. Also a CLI: `rezextract.py list <rez>`. |
| `dsp2files.py` | `build/build.sh` | Lists a VC6 `.dsp` project's source files for one configuration, so the game DLLs compile exactly the files the original project did. |
| `mkimplib.py` | `build/engine.sh` | Writes a COFF import library for a few named imports (Miles' decorated names, `DirectDrawCreateEx`). |
| `mkaliasobj.py` | `build/engine.sh` | Turns `src/engine/*/link/aliases.rsp` into an object of weak externals: one function or global reached under two names. |
| `mkvtabobj.py` | `build/engine.sh` | Turns `src/engine/lithtech/link/vtables.txt` into an object holding full-length vtables for classes the source only partly declares. |
| `mkviewobj.py` | `build/engine.sh` | Turns `src/engine/d3d/link/d3dviews.txt` into an object laying out `d3d.ren`'s globals as the original did. |
| `playtest.sh` | `make test` | Runs a game directory under Xvfb in the build container, optionally driving it with an input script, and reports ALIVE / EXITED / CRASHED (plus `wine.log` and screenshots in `out/play/<name>/`). Usage in its header. |
| `playtest-basic.txt` | `make test` | The input script: skip the opening cinematic, look, move, jump, duck, fire, switch weapons, open the menu, quicksave and quickload. |
