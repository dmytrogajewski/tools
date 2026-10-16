#!/usr/bin/env bash
# servertest.sh -- the dedicated server's runtime test: does NolfServ.exe host
# a game, and can the game join it?
#
#   tools/servertest.sh [gamedir] [seconds]
#   SERVERTEST_CLIENT=1 tools/servertest.sh [gamedir] [seconds]
#
# gamedir:    a complete game directory, as build/all.sh assembles it
#             (default out/nolf-legacy-release), with its NolfServ.exe. It is
#             tested exactly as it is: every binary in it runs, nothing is
#             substituted or filled in.
# seconds:    how long the server (and the client) run before the verdict
#             (default 60)
#
# Runs inside the build container under Xvfb, in its own directory
# out/play/<name>/ (<name> is the game directory's basename with -server
# appended, or $SERVERTEST_NAME), set up as tools/playtest.sh sets up a run:
# the binaries copied, the data linked (so the game directory's
# modern-server.log and modern.log, in the modern build, are the run's),
# autoexec.cfg and Save/ private. The
# server starts unattended: a fresh NetHost.txt (a deathmatch on the worlds
# $SERVERTEST_LEVELS, default 001THEPAD_DM and 002REBAR_DM, port 27888) and
# `NolfServ.exe -nowiz -NetSessionName servertest` (the retail parser reads
# -nowiz only when another argument follows it: RETAIL_BUGS.md). It is
# hosting once its UDP port is open. With SERVERTEST_CLIENT=1 the game then
# starts too, joining it (+connect 127.0.0.1:27888, in a window).
#
# Writes, in that directory:
#   server.log, client.log   what Wine and each program printed
#   server.png, client.png   each program's window at the end
#   result.txt               PASS / FAIL, and why
# and prints result.txt (and the end of the modern build's logs: the
# server's modern-server.log, the client's modern.log). Exit status 0 only for PASS: the server hosted and was still running
# at the end (and so was the client, if one was started).
#
# Optional settings, from the environment:
#   SERVERTEST_ARGS       more NolfServ.exe arguments (after the two above)
#   SERVERTEST_LEVELS     the worlds, space separated
#   SERVERTEST_GAMETYPE   2 deathmatch (default), 1 H.A.R.M. vs UNITY
#   SERVERTEST_NEXTLEVEL=1  half way through, click the server's Next Level
#                         (the level change the game's players then follow)
set -u
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
for t in wine xvfb-run xdotool xwd convert; do
    command -v $t >/dev/null 2>&1 || { echo "servertest.sh: no $t here -- run it in the build container (make shell)"; exit 2; }
done
GAMEDIR="${1:-out/nolf-legacy-release}"
SECS="${2:-60}"
abs() { case "$1" in /*) echo "$1" ;; *) echo "$REPO/$1" ;; esac; }
GAME="$(abs "$GAMEDIR")"
for f in NolfServ.exe server.dll lithtech.exe nolfcmds.txt startup.txt defaults.cfg; do
    [ -f "$GAME/$f" ] || { echo "$GAME/$f missing -- not a game directory with the server (build/all.sh makes one)"; exit 2; }
done
NAME="${SERVERTEST_NAME:-$(basename "$GAME")-server}"
LEVELS="${SERVERTEST_LEVELS:-Worlds\\Multi\\DeathMatch\\001THEPAD_DM Worlds\\Multi\\DeathMatch\\002REBAR_DM}"
GAMETYPE="${SERVERTEST_GAMETYPE:-2}"

RUN="$REPO/out/play/$NAME"
rm -rf "$RUN"; mkdir -p "$RUN"
REL="$(python3 -c 'import os,sys; print(os.path.relpath(sys.argv[1], sys.argv[2]))' "$GAME" "$RUN")"
for f in "$GAME"/*; do
    b=$(basename "$f")
    case "$b" in autoexec.cfg|Save|nolfcmds.txt|NetHost.txt|MANIFEST.txt|linux-run.sh|linux-run-launcher.sh|run-cmds.txt|cd) continue ;; esac
    if [ -f "$f" ] && [ "$(head -c 2 "$f" | tr -d '\0')" = MZ ]; then cp "$f" "$RUN/$b"
    else ln -s "$REL/$b" "$RUN/$b"; fi
done
echo "game directory: $GAME" > "$RUN/binaries.txt"
cp "$GAME/defaults.cfg" "$RUN/autoexec.cfg"
mkdir "$RUN/Save"
# The server's settings, the "Host" tag NolfServ.exe keeps in NetHost.txt
{
    printf '[Host]\r\n'
    printf 'NetGameType = %d.000000f\r\n' "$GAMETYPE"
    printf 'NetSessionName = "servertest"\r\n'
    printf 'NetPort = 27888.000000f\r\n'
    printf 'NetMaxPlayers = 8.000000f\r\n'
    if [ "$GAMETYPE" = 1 ]; then pre=NetCALevel; num=NetCANumLevels; else pre=NetLevel; num=NetNumLevels; fi
    n=0
    for w in $LEVELS; do printf '%s%d = "%s"\r\n' $pre $n "$w"; n=$((n+1)); done
    printf '%s = %d.000000f\r\n' $num $n
} > "$RUN/NetHost.txt"
# The client joins: the game directory's command file, plus +connect
printf '%s +connect 127.0.0.1:27888\r\n' "$(tr -d '\r\n' < "$GAME/nolfcmds.txt")" > "$RUN/nolfcmds.txt"

cat > "$RUN/runner.sh" <<'RUNNER'
#!/bin/bash
SECS=$1; CLIENT=$2; shift 2
log() { echo "$(date +%T) $*" >> steps.txt; }
wine NolfServ.exe -nowiz -NetSessionName servertest "$@" > server.log 2>&1 &
SPID=$!
# hosting: the server's UDP port (27888 = 0x6CF0) is open
HOSTED=""
for i in $(seq 1 120); do
    if grep -qi ':6CF0 ' /proc/net/udp 2>/dev/null; then HOSTED=1; break; fi
    kill -0 $SPID 2>/dev/null || break
    sleep 0.5
done
if [ -n "$HOSTED" ]; then log "server: hosting on UDP 27888"; else log "server: not hosting"; fi
CPID=""
if [ -n "$HOSTED" ] && [ "$CLIENT" = 1 ]; then
    sleep 5
    wine lithtech.exe -cmdfile nolfcmds.txt > client.log 2>&1 &
    CPID=$!
    log "client: started"
fi
click() {  # click <window> <dialog x> <dialog y>: the server dialog is 399x297 units
    eval "$(xdotool getwindowgeometry --shell "$1" 2>/dev/null)"
    xdotool mousemove $(( X + WIDTH * $2 / 399 )) $(( Y + HEIGHT * $3 / 297 )) click 1
}
for i in $(seq 1 $SECS); do
    sleep 1
    kill -0 $SPID 2>/dev/null || break
    [ -n "$CPID" ] && { kill -0 $CPID 2>/dev/null || break; }
    if [ "${NEXTLEVEL:-}" = 1 ] && [ $i = $(( SECS / 2 )) ]; then
        W=$(xdotool search --name '^NOLF Server' 2>/dev/null | head -1)
        [ -n "$W" ] && { xdotool windowraise "$W" 2>/dev/null; sleep 0.5; click "$W" 256 177; log "server: Next Level"; }
    fi
done
shot() {  # shot <window name regex> <file>: raised, then captured
    local w
    w=$(xdotool search --name "$1" 2>/dev/null | head -1)
    [ -n "$w" ] || return
    xdotool windowraise "$w" 2>/dev/null; sleep 1
    xwd -id "$w" -silent | convert xwd:- "$2" 2>/dev/null
}
shot '^NOLF Server' server.png
[ -n "$CPID" ] && shot '^LithTech$' client.png
xwd -root -silent | convert xwd:- screen.png 2>/dev/null
SALIVE=""; kill -0 $SPID 2>/dev/null && SALIVE=1
CALIVE=""; [ -n "$CPID" ] && kill -0 $CPID 2>/dev/null && CALIVE=1
# The server is then stopped as its operator would: Escape (Stop Server)
# and Return to confirm, and must exit by itself, without a crash. (The
# client goes first: its window would keep the keyboard.)
[ -n "$CALIVE" ] && { kill $CPID 2>/dev/null; sleep 3; }
STOPPED=""
if [ -n "$SALIVE" ]; then
    W=$(xdotool search --name '^NOLF Server' 2>/dev/null | head -1)
    if [ -n "$W" ]; then
        xdotool windowraise "$W" windowfocus "$W" 2>/dev/null; sleep 1
        click "$W" 361 177; sleep 2    # its Stop Server button
        xwd -root -silent | convert xwd:- stop.png 2>/dev/null
        for b in $(xdotool search --name '.' 2>/dev/null); do
            log "  window $b: $(xdotool getwindowname $b 2>/dev/null)"
        done
        for b in $(xdotool search --name '.' 2>/dev/null); do
            case "$(xdotool getwindowname $b 2>/dev/null)" in
                NolfServer*|NOLF*Server) [ "$b" != "$W" ] && { xdotool windowfocus $b 2>/dev/null; xdotool key --window $b Return; } ;;
            esac
        done
        for i in $(seq 1 30); do sleep 0.5; kill -0 $SPID 2>/dev/null || break; done
        kill -0 $SPID 2>/dev/null || STOPPED=1
    fi
    if [ -n "$STOPPED" ]; then log "server: stopped and exited"; else log "server: did not exit when stopped"; fi
fi
WHY=""
[ -n "$HOSTED" ] || WHY="$WHY; the server never opened its port"
[ -n "$SALIVE" ] || WHY="$WHY; the server exited"
[ -z "$SALIVE" ] || [ -n "$STOPPED" ] || WHY="$WHY; the server did not exit when stopped"
if [ "$CLIENT" = 1 ] && [ -n "$HOSTED" ]; then
    [ -n "$CALIVE" ] || WHY="$WHY; the client exited"
fi
if [ -z "$WHY" ]; then
    echo "PASS: the server hosted${CPID:+ and the client ran} for ${SECS}s, and stopped cleanly" > result.txt
else
    echo "FAIL${WHY#;}" > result.txt
fi
if grep -qiE "unhandled exception|page fault" server.log 2>/dev/null; then
    sed -i 's/^PASS:.*/FAIL: the server crashed/' result.txt
fi
grep -iE "unhandled exception|page fault" server.log client.log 2>/dev/null | head -3 >> result.txt
wineserver -k; sleep 2
RUNNER

(
cd "$RUN" || exit 1
export WINEDEBUG=${SERVERTEST_WINEDEBUG:--all} WINEDLLOVERRIDES=${SERVERTEST_DLLOVERRIDES-winedbg.exe=d}
NEXTLEVEL="${SERVERTEST_NEXTLEVEL:-}" xvfb-run -a -s "-screen 0 1280x1024x24" bash runner.sh "$SECS" "${SERVERTEST_CLIENT:-0}" ${SERVERTEST_ARGS:-}
)
cat "$RUN/result.txt" 2>/dev/null || echo "NO RESULT (the run failed)"
for f in modern-server.log modern.log; do
    if [ -L "$RUN/$f" ] || [ -f "$RUN/$f" ]; then
        echo "-- $f (end):"
        tail -20 "$RUN/$f" 2>/dev/null
    fi
done
grep -q '^PASS' "$RUN/result.txt" 2>/dev/null
