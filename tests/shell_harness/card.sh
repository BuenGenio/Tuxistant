#!/bin/bash
# The approval card in an isolated headless GNOME Shell.
#
#   tests/shell_harness/card.sh <theme-id> <out-dir>
#
# Runs card_daemon.py against the real extension and writes report.json, PNGs and shell.log to
# <out-dir>. Exit 0: clean run. 2: the run finished but the shell log has errors. 1: failed.
#
# Environment:
#   TUX_SCENARIO      card_daemon.py scenario, default verify
#   TUX_DRIVER        script in this folder to run in place of card_daemon.py
#   TUX_MONITORS      virtual monitors, space separated, default 1600x900
#   TUX_SCALE         integer UI scale, default 1
#   TUX_ANIMATIONS    false to run with animations off, as a shell without 3D does
#   TUX_EXTENSION     extension folder to test instead of extension/tuxistant@buengenio
#   TUX_EXTRA_THEMES  folder of extra <id>/ themes, used as user themes in this run
#   TUX_TIMEOUT       seconds for the whole run, default 150
#   TUX_KEEP          set to keep the temp folder (printed at the end)
#
# The isolation is shot.sh's: own runtime, config, data and cache folders, a private session bus
# and a private empty system bus, so nothing here can reach the real desktop or its clipboard.
set -u

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
root=$(cd "$here/../.." && pwd)

if [ "${1:-}" = --inner ]; then
    # Runs under dbus-run-session. Every process here is a child of this script.
    work=$2 theme=$3 out=$4 scenario=$5
    sysbus='' daemon='' shell=''
    stop_children() {
        trap - EXIT
        for pid in $daemon $shell $sysbus; do kill "$pid" 2>/dev/null; done
        for _ in $(seq 30); do
            [ -n "$shell" ] && kill -0 "$shell" 2>/dev/null || break
            sleep 0.1
        done
        for pid in $daemon $shell $sysbus; do kill -9 "$pid" 2>/dev/null; done
    }
    trap stop_children EXIT
    trap 'exit 1' INT TERM

    dbus-daemon --config-file="$work/bus.conf" --nofork --print-address=1 >"$work/system-bus" &
    sysbus=$!
    for _ in $(seq 100); do [ -s "$work/system-bus" ] && break; sleep 0.05; done
    export DBUS_SYSTEM_BUS_ADDRESS
    DBUS_SYSTEM_BUS_ADDRESS=$(head -n1 "$work/system-bus")

    export TUX_WAYLAND_DISPLAY="tux-$$"
    python3 -B "$here/$TUX_DRIVER" --theme "$theme" --out "$out" --scenario "$scenario" \
        >"$work/daemon.log" 2>&1 &
    daemon=$!
    for _ in $(seq 200); do
        grep -q 'ready for the shell' "$work/daemon.log" && break
        kill -0 "$daemon" 2>/dev/null || break
        sleep 0.05
    done

    if kill -0 "$daemon" 2>/dev/null; then
        monitors=()
        for monitor in $TUX_MONITORS; do monitors+=(--virtual-monitor "$monitor"); done
        gnome-shell --headless --wayland --no-x11 --wayland-display "$TUX_WAYLAND_DISPLAY" "${monitors[@]}" \
            >"$work/shell.log" 2>&1 &
        shell=$!
    fi
    wait "$daemon"
    status=$?
    daemon=''
    exit $status
fi

if [ $# -ne 2 ]; then
    sed -n '2,6p' "$0" | cut -c3-
    exit 1
fi
theme=$1
out=$(realpath -m "$2")
mkdir -p "$out"

# Always /tmp: socket paths have a 108 byte limit that a long TMPDIR would break.
work=$(mktemp -d /tmp/tux-card.XXXXXX)
mkdir -m 700 "$work/run"
mkdir -p "$work/home" "$work/cache" "$work/state" "$work/config/glib-2.0/settings" \
    "$work/data/gnome-shell/extensions" "$work/data/tuxistant/themes" "$work/empty"
cp -r "${TUX_EXTENSION:-$root/extension/tuxistant@buengenio}" "$here/tuxistant-shot@test" \
    "$work/data/gnome-shell/extensions/"
if [ -n "${TUX_EXTRA_THEMES:-}" ]; then
    cp -r "$TUX_EXTRA_THEMES"/. "$work/data/tuxistant/themes/"
fi

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
enable-animations=${TUX_ANIMATIONS:-true}
enable-hot-corners=false
color-scheme='prefer-dark'
scaling-factor=uint32 ${TUX_SCALE:-1}

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
env -i PATH="$PATH" HOME="$work/home" USER="${USER:-tux}" LOGNAME="${LOGNAME:-tux}" LANG=C.UTF-8 \
    XDG_RUNTIME_DIR="$work/run" XDG_CONFIG_HOME="$work/config" XDG_DATA_HOME="$work/data" \
    XDG_CACHE_HOME="$work/cache" XDG_STATE_HOME="$work/state" GSETTINGS_BACKEND=keyfile \
    XDG_CURRENT_DESKTOP=GNOME XDG_SESSION_TYPE=wayland PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUSERBASE="$userbase" TUX_MONITORS="${TUX_MONITORS:-1600x900}" \
    TUX_DRIVER="${TUX_DRIVER:-card_daemon.py}" \
    timeout -k 8 "${TUX_TIMEOUT:-150}" dbus-run-session --config-file="$work/bus.conf" -- \
    "$here/card.sh" --inner "$work" "$theme" "$out" "${TUX_SCENARIO:-verify}" &
outer=$!
wait "$outer"
status=$?
outer=''

[ -s "$work/daemon.log" ] && grep -v 'ready for the shell' "$work/daemon.log" >&2
cp "$work/shell.log" "$out/" 2>/dev/null

errors=$(grep -E 'Gjs-CRITICAL|Gjs-WARNING|JS ERROR|JS WARNING|SyntaxError|TypeError|ReferenceError|[Ee]xtension .*(error|Error)|[Tt]uxistant|St-CRITICAL|Clutter-CRITICAL|GLib-CRITICAL' \
    "$work/shell.log" 2>/dev/null)
if [ -n "$errors" ]; then
    echo "--- shell log problems ---" >&2
    echo "$errors" >&2
fi

if [ "$status" -ne 0 ]; then
    echo "FAILED (status $status)" >&2
    [ -z "$errors" ] && tail -n 30 "$work/shell.log" >&2 2>/dev/null
    exit 1
fi
[ -n "$errors" ] && exit 2
exit 0
