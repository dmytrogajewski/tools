#!/usr/bin/env bash
# Measure every src/engine/d3d translation unit at /O2 and at /Os and report
# which one reproduces more of the shipped bytes.
#
# d3d.ren is a MIXED build: its real translation units do not share one flag
# set, and OUR file boundaries are invented and do not line up with the
# shipped ones. So the split is measured, never derived -- and it has to be
# re-measured whenever functions move between files. Doing that by hand
# leaves files on the wrong flag.
#
# Prints a ready-to-paste list for the D3D_OS group (build/build.mk). Run it in the
# build container, after build/reference.sh:
#   make shell, then: tools/d3dflags.sh
set -u
cd "$(dirname "$0")/.." || exit 1
export WINEDEBUG=-all
source build/env.sh

best=()
for f in src/engine/d3d/*.cpp; do
    tu=$(basename "$f")
    stem=${tu%.cpp}
    declare -A n=() d=()
    for F in /O2 /Os; do
        obj=src/engine/d3d/Release/"$stem".obj
        rm -f "$obj"
        ( cd src/engine/d3d && run_icl /nologo /MT /W3 "$F" /D WIN32 /D NDEBUG \
            /D _WINDOWS /D NO_PRAGMA_LIBS /I ..\\..\\LT2\\sdk\\inc /I . \
            /c /FoRelease/"$stem".obj "$tu" ) >/dev/null 2>&1
        # The object is DELETED before each compile and its existence tested
        # after. Measuring without this test silently scores the PREVIOUS
        # flag's object whenever a compile fails, which has produced a wrong
        # answer twice -- both times believed, because a number came back.
        if [ ! -f "$obj" ]; then n[$F]=-1; d[$F]=999999; continue; fi
        out=$(python3 tools/d3dcheck.py --obj="$stem" 2>/dev/null)
        n[$F]=$(printf '%s' "$out" | head -1 | grep -oP '^\s*\K[0-9]+')
        n[$F]=${n[$F]:-0}
        # Total differing bytes, which is what breaks a tie. A file where
        # NOTHING matches at either flag ties at 0-0 forever, and ranking on
        # match count alone left d3dblit.cpp on /O2 while
        # every one of its thirteen functions was better at /Os.
        d[$F]=$(printf '%s' "$out" | grep -oP '[0-9]+(?= byte\(s\) differ)' |
                awk '{s+=$1} END {print s+0}')
        d[$F]=${d[$F]:-0}
    done
    mark=""
    if [ "${n[/Os]}" -gt "${n[/O2]}" ]; then
        best+=("$tu"); mark="  <- /Os"
    elif [ "${n[/Os]}" -eq "${n[/O2]}" ] && [ "${d[/Os]}" -lt "${d[/O2]}" ]; then
        best+=("$tu"); mark="  <- /Os (on differing bytes, matches tied)"
    fi
    printf "  %-22s /O2 %3s (%6s b)   /Os %3s (%6s b)%s\n" \
        "$tu" "${n[/O2]}" "${d[/O2]}" "${n[/Os]}" "${d[/Os]}" "$mark"
done

echo
echo "D3D_OS group (paste into build/build.mk):"
printf '%s\n' "${best[@]}"
echo
echo "NOTE: every object left in Release/ is now whichever flag was measured"
echo "last. Re-run build/engine.sh d3d before trusting any other measurement."
