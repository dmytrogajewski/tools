# 14 · The scoreboard: building and using `progress/`

Every other guide is about one function. This one is about all of them at
once: how the project measures how much of the retail game it rebuilds
exactly, how that measure is held so it can only go up, and how you use it
to pick work and to hand work in.

The reference for every status, file and rule is the host project's
[`progress/README.md`](../../progress/README.md). This guide is the
walk-through: what to run, in what order, what the output means, and what to
do when it fails.

## The pieces

```
  the retail images ──┐
  (out/reference/)    │
                      ├─▶ tools/progress.py ─▶ out/progress/  ─┐
  our build output ───┤    (score this run)    functions.csv   │
  (src/*/Release/,    │                        summary.json    │
   src/lithshared/out)│                                        ▼
                      │                        tools/ratchet.py check|update
  progress/*.csv ─────┘                                        │
  (hand-curated                     progress/identical.txt ◀───┤ (the baseline)
   evidence)                        progress/summary.json  ◀───┤
                                    README.md's table      ◀───┘
```

* **`tools/progress.py`** scores one run. It enumerates every function of
  every retail binary from the binary itself, searches our objects for each
  one, and gives every shipped function one status (`identical`, `near`,
  `small`, `attempted`, `todo`, ...). It writes `out/progress/`, which is a
  scratch result: nothing there is committed.
* **`tools/ratchet.py`** compares that run with the **baseline** committed in
  `progress/`: `identical.txt` (every function ever verified identical) and
  `summary.json` (the figures they add up to, from which the README table is
  rendered). `check` fails on any regression; `update` records the run as the
  new baseline.
* **`progress/*.csv`** are evidence a human gathered: addresses the
  enumeration gets wrong, function starts it misses, extents it misjudges.
  They change the denominator, so they are reviewed like code.
* **`build/progress.sh`** is the glue, `make progress` and `make
  progress-update` its entry points.

The scoring compiles nothing. It reads whatever the last build left in the
tree, so the order is always: build, then score.

## Before the first run

You need three things.

1. **The build**: `make`. The scorer reads `src/engine/*/Release/*.obj`,
   `src/game/*/Final_Release/*.obj`, the `.map` files of our own links and
   `src/lithshared/out/*.lib`. Only the release build is scored; the variant
   builds live under `out/build-<variant>/`, where the scorer never looks.
2. **The retail images**, the ground truth. `make retail` installs the
   retail game (from `rez/`), and the first `make progress` extracts the
   binaries it scores into `out/reference/` (`build/reference.sh`), with a
   function and vtable table per image. That happens once.
3. **Optionally, the source release's libraries.** The seven lithshared
   libraries are scored against the prebuilt `.lib`s from Monolith's *NOLF
   Source Code v1.003* release, which is not in the repository. Point
   `NOLF_SOURCE` at your extracted copy (the directory holding `LT2/`). They
   are checked against `progress/shipped-libs.sha256`, and a mismatch is an
   error, not a skip: a different library would be the wrong ground truth.
   Without `NOLF_SOURCE` everything else is still scored and checked.

## Checking: `make progress`

```sh
make                                   # build what you changed
make progress                          # score it, hold it to the baseline
make progress NOLF_SOURCE=~/nolfsrc    # the same, with the libraries
```

`make progress` runs `build/progress.sh check` in the build container:
`progress.py`, then `ratchet.py check`. It prints a line per target as it
places functions, then the score of the run, then the run against the
baseline and the verdict (trimmed; a run without `NOLF_SOURCE`):

```
target              ident  near  small  attem  todo  funcs  unwr  unpl   bytes id  bytes tot      %
---------------------------------------------------------------------------------------------------------
NOLF.exe              172     1     48      0     0    221     0     0      34056      35033  97.2%
server.dll           2031    47    194      0     0   2272     0     4     212941     249622  85.3%
...
wrote out/progress/functions.csv, out/progress/summary.json

target                identical (recorded -> now)    identical bytes (recorded -> now)      unwritten       unplaced
NOLF.exe                    172 ->    172                  34056 ->    34056                0 -> 0         0 -> 0
server.dll                 2031 ->   2031                 212941 ->   212941                0 -> 0         4 -> 4
...
PASS: every function recorded as byte-identical still is
```

The first table is this run; the second is this run against the baseline,
and the one to read after a change. It fails (non-zero exit) when:

* a function listed in `progress/identical.txt` is no longer identical,
* a target's identical byte count went down,
* a target's count of **unwritten** or **unplaced** functions went up, or
* `README.md`'s table is not the one `progress/summary.json` renders (someone
  edited one without the other).

New identical functions **pass**, with a reminder that they are not recorded
yet. Run it before you hand anything in; CI runs it on every push and pull
request (`.github/workflows/public-archives.yml`, without the libraries).

Two stricter forms exist for later: `make progress COVERED=1` also fails
unless nothing is unwritten or unplaced anywhere (every shipped function has
a written, placed counterpart), and `make progress COMPLETE=1` unless every
function is identical -- the finish line.

## Recording: `make progress-update`

When a run is better than the baseline, lock it in:

```sh
make progress-update NOLF_SOURCE=~/nolfsrc
git add progress/identical.txt progress/summary.json README.md
```

It rewrites `progress/identical.txt`, `progress/summary.json` and the README
table, so from then on the new functions are held like the old ones. Commit
the three files with the source change that earned them, so the history
shows which change made which function identical.

It refuses two kinds of run:

* **A run without the libraries.** The baseline covers every target, so it
  needs `NOLF_SOURCE`.
* **A run that lost a recorded function.** Fix the loss, or -- when the change
  rightly gives the function up (it was identical by coincidence, say, and
  the right source compiles differently) -- record it with
  `make progress-update ACCEPT_LOSSES=1` and say why in the commit message.

## Reading a run

`out/progress/functions.csv` has one row per shipped function: target,
name, size, status. `progress.py` has views onto it, inside `make shell` (it
needs Python 3 only):

```sh
python3 tools/progress.py --nears=server.dll     # near matches, closest first
python3 tools/progress.py --todo=lithtech.exe    # untouched, largest first
python3 tools/progress.py --attempted=d3d.ren    # worked on, not matched
python3 tools/progress.py --unplaced=d3d.ren     # ours, with no shipped home
```

Which list to work from depends on the kind of work:

| You want to | Start from | Guide |
|---|---|---|
| close functions that are almost right | `--nears`, smallest delta first | [10-upgrading-nears](10-upgrading-nears.md) |
| write something new | `--todo`, a small function that opens a cluster | [README](README.md#picking-a-target), [13-cluster-recon](13-cluster-recon.md) |
| fix coverage | `--unplaced` and `--todo` of the same target, side by side | below |

The two coverage counts, per target in the README:

* **Unwritten**: shipped functions with nothing of ours placed at them
  (`todo` + `attempted`).
* **Unplaced**: functions of ours, in our own link of the image, that no
  shipped function is placed for.

A function written right but compiled differently -- other registers, another
block order -- is placed by similarity, or joined to its shipped twin by
name (`sub_<addr>`), by object (the linker keeps an object's functions
together) or by link order. When all of that misses it, it is counted twice:
once unwritten, once unplaced. So an unplaced function is usually a near
match the scorer could not see. Naming it for the address it reconstructs
(`sub_<addr>`, `d3d.ren`'s convention) joins the two; a comment in `src/`
citing the address only marks the shipped row `attempted`.

## When the enumeration is wrong: the CSVs

The retail images have no symbols. Their functions are found from the code
(call targets, vtables, exports, callbacks), and that occasionally finds
something that is not a function -- an operand byte that looked like a call
target, a jump into the middle of another function, padding -- or misses one
hidden behind a thunk. Since the denominator comes from this enumeration, a
wrong row is a wrong score. The fixes are rows in `progress/`:

| File | Add a row when |
|---|---|
| `<image>-screen-exclude.csv` | an enumerated address is not a function (reason starting `NOT A FUNCTION` or `NOT GAME CODE`: removed from the count), or you examined it by hand (any other reason: marked `attempted`) |
| `extent-splits.csv` | a function start the enumeration misses |
| `<image>-enum-suspect.csv` | a row looks wrong but you have not proved it: kept in the count, left off the worklists of `uncovered.py`, `decoded.py` and `d3dwork.py` |
| `decoded-extents.csv` | a large function whose enumerated extent is wrong |

Every row carries its evidence in the reason column -- the bytes, the
instruction that jumps there, the function it belongs to -- as the existing
rows do. A reviewer has to be able to check it without redoing your work.
Changing the denominator without evidence is the one way to make the
scoreboard lie.

## A session, start to finish

```sh
make                                          # 1. a clean baseline build
make progress                                 # 2. passes: you start from green
python3 tools/progress.py --nears=server.dll  # 3. pick work (in make shell)
#    ... write source; build/engine.sh server-one <file> <symbol>; fndiff ...
make                                          # 4. rebuild everything
make progress                                 # 5. no regressions, new functions reported
make progress-update NOLF_SOURCE=~/nolfsrc    # 6. record them
git add -A src progress README.md && git commit
```

If step 2 fails on a fresh checkout, the tree and the baseline disagree
before you have touched anything: the build is not the one the baseline was
recorded from (another compiler, a stale object). Fix that first -- every
later result depends on it.

## Adapting it to another project

`progress.py` and `ratchet.py` are general in method and specific in their
tables. To score another binary or another game, the tables to change are at
the top of each:

* `progress.py`: `REFERENCE` (where the retail images are), `ENGINE` and
  `GAME` (each target: label, shipped image, our objects), `LIBS` and
  `SHIPPED_LIB` (libraries scored symbol by symbol against shipped `.lib`s),
  `APP_REGION` and `APP_ISLANDS` (the address range of the developer's own
  code in each image: everything else is the statically linked runtime,
  listed as `crt` and not counted), `MIDDLEWARE` (third-party code inside
  that range), `OUR_MAP` (our own link's `.map` per image), `DRIVER_OBJS`
  and `_EXCLUDE_IMAGE` (which CSV prefix belongs to which image).
* `ratchet.py`: `LIBS` and `ROWS` (the README table's rows and what each adds
  up), and `README`, `BEGIN` and `END` (where the table goes).

Get `APP_REGION` right first, with evidence: it decides what the denominator
is, and counting the runtime would understate progress as badly as counting
only what has been attempted would overstate it. Then record a first
baseline with `ratchet.py update`, and from then on the ratchet holds it.
