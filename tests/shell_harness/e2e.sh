#!/bin/bash
# End to end in an isolated headless GNOME Shell: the real daemon, the extension as installed by
# install.sh, the real Settings app and Fn helper, the live server, and scripted audio.
#
#   TUXISTANT_API_KEY=... [TUXISTANT_SERVER=host:8080] tests/shell_harness/e2e.sh <out-dir>
#
# Writes report.json and PNGs to <out-dir>. Exit 0: every check passed and the shell log is clean.
# 2: the checks passed but the shell log has errors. 1: a check failed.
#
# Environment:
#   TUXISTANT_SERVER  the LocalAI server, default the daemon's own default, localhost:8080
#   TUX_KEEP       set to keep the temp folder, minus the API key
#   TUX_TIMEOUT    seconds for the whole run, default 480
#   TUX_ONLY       comma separated scenario steps, default all
#
# The same isolation as shot.sh: own runtime, config, data and cache folders, a private session
# bus and a private empty system bus. The key only ever lives in a 0600 file in the temp folder.
set -u

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
root=$(cd "$here/../.." && pwd)

if [ "${1:-}" = --inner ]; then
    work=$2 out=$3
    sysbus='' shell=''
    stop_children() {
        trap - EXIT
        for pid in $shell $sysbus; do kill "$pid" 2>/dev/null; done
        for _ in $(seq 30); do
            [ -n "$shell" ] && kill -0 "$shell" 2>/dev/null || break
            sleep 0.1
        done
        for pid in $shell $sysbus; do kill -9 "$pid" 2>/dev/null; done
    }
    trap stop_children EXIT
    trap 'exit 1' INT TERM

    dbus-daemon --config-file="$work/bus.conf" --nofork --print-address=1 >"$work/system-bus" &
    sysbus=$!
    for _ in $(seq 100); do [ -s "$work/system-bus" ] && break; sleep 0.05; done
    export DBUS_SYSTEM_BUS_ADDRESS
    DBUS_SYSTEM_BUS_ADDRESS=$(head -n1 "$work/system-bus")

    gnome-shell --headless --wayland --no-x11 --wayland-display "tux-e2e" \
        --virtual-monitor 1600x900 >"$work/shell.log" 2>&1 &
    shell=$!
    python3 -B "$here/e2e_scenario.py" --work "$work" --out "$out"
    exit $?
fi

if [ $# -ne 1 ] || [ -z "${TUXISTANT_API_KEY:-}" ]; then
    sed -n '2,12p' "$0" | cut -c3-
    exit 1
fi
out=$(realpath -m "$1")
mkdir -p "$out"

# Always /tmp: socket paths have a 108 byte limit that a long TMPDIR would break.
work=$(mktemp -d /tmp/tux-e2e.XXXXXX)
mkdir -m 700 "$work/run"
mkdir -p "$work/home" "$work/cache" "$work/state" "$work/config/glib-2.0/settings" \
    "$work/data" "$work/bin" "$work/empty" "$work/audio"

cat >"$work/config/glib-2.0/settings/keyfile" <<KEYFILE
[org/gnome/shell]
enabled-extensions=['tuxistant@buengenio', 'tuxistant-shot@test']
disable-user-extensions=false
welcome-dialog-last-shown-version='9999'

[org/gnome/desktop/background]
picture-uri=''
picture-uri-dark=''
picture-options='none'
color-shading-type='solid'
primary-color='#3a4150'

[org/gnome/desktop/interface]
enable-animations=true
enable-hot-corners=false
color-scheme='prefer-dark'

[org/gnome/desktop/screensaver]
lock-enabled=false
idle-activation-enabled=false

[org/gnome/desktop/session]
idle-delay=uint32 0
KEYFILE

cat >"$work/bus.conf" <<BUS
<!DOCTYPE busconfig PUBLIC "-//freedesktop//DTD D-Bus Bus Configuration 1.0//EN"
 "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">
<busconfig>
  <type>session</type>
  <listen>unix:dir=$work/run</listen>
  <auth>EXTERNAL</auth>
  <servicedir>$work/empty</servicedir>
  <policy context="default">
    <allow send_destination="*" eavesdrop="true"/>
    <allow eavesdrop="true"/>
    <allow own="*"/>
  </policy>
</busconfig>
BUS

outer=''
cleanup() {
    trap - EXIT INT TERM
    if [ -n "$outer" ]; then
        kill "$outer" 2>/dev/null
        wait "$outer" 2>/dev/null
    fi
    rm -f "$work/config/tuxistant/config.json"
    if [ -n "${TUX_KEEP:-}" ]; then
        echo "kept: $work" >&2
    else
        rm -rf "$work"
    fi
}
trap cleanup EXIT
trap 'exit 1' INT TERM

# The daemon needs its Python modules from the user's site folder while HOME points elsewhere.
userbase=$(python3 -m site --user-base)
isolated=(HOME="$work/home" XDG_CONFIG_HOME="$work/config" XDG_DATA_HOME="$work/data"
    XDG_BIN_HOME="$work/bin" XDG_CACHE_HOME="$work/cache" XDG_STATE_HOME="$work/state"
    PYTHONUSERBASE="$userbase" PYTHONDONTWRITEBYTECODE=1)

env -i PATH="$PATH" "${isolated[@]}" TUXISTANT_INSTALL_DRYRUN=1 "$root/install.sh" \
    >"$work/install.log" 2>&1 || { cat "$work/install.log" >&2; exit 1; }
cp -r "$here/tuxistant-shot@test" "$work/data/gnome-shell/extensions/"
python3 -c '
import json, os, sys
path = sys.argv[1]
config = json.load(open(path))
config["api_key"] = os.environ["TUXISTANT_API_KEY"]
if os.environ.get("TUXISTANT_SERVER"):
    config["server"] = os.environ["TUXISTANT_SERVER"]
with os.fdopen(os.open(path, os.O_WRONLY | os.O_TRUNC), "w") as f:
    json.dump(config, f)' "$work/config/tuxistant/config.json"

env -i PATH="$PATH" "${isolated[@]}" USER="${USER:-tux}" LOGNAME="${LOGNAME:-tux}" LANG=C.UTF-8 \
    XDG_RUNTIME_DIR="$work/run" GSETTINGS_BACKEND=keyfile XDG_CURRENT_DESKTOP=GNOME \
    XDG_SESSION_TYPE=wayland TUX_ONLY="${TUX_ONLY:-}" \
    timeout -k 8 "${TUX_TIMEOUT:-480}" dbus-run-session --config-file="$work/bus.conf" -- \
    "$here/e2e.sh" --inner "$work" "$out" &
outer=$!
wait "$outer"
status=$?
outer=''

cp "$work/install.log" "$work/shell.log" "$work/daemon.log" "$out/" 2>/dev/null
errors=$( { grep -E -A14 'Shader compilation failed|Failed to link GLSL|shader.*(error|failed)' "$work/shell.log"
    grep -E 'Gjs-CRITICAL|JS ERROR|JS WARNING|SyntaxError|TypeError|ReferenceError|[Ee]xtension .*(error|Error)|[Tt]uxistant' "$work/shell.log"
} 2>/dev/null )
if [ -n "$errors" ]; then
    echo "--- shell log problems ---" >&2
    echo "$errors" >&2
fi
[ "$status" -ne 0 ] && { echo "FAILED (status $status)" >&2; exit 1; }
[ -n "$errors" ] && exit 2
exit 0
