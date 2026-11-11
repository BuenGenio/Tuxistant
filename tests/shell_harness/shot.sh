#!/bin/bash
# Screenshot a theme in an isolated headless GNOME Shell.
#
#   tests/shell_harness/shot.sh <theme-id> <out-dir>
#
# Writes listening.png, thinking.png and speaking.png to <out-dir> (1600x900). Prints GLSL and JS
# errors from the shell log. Exit 0: clean run. 2: the shots exist but the log has errors. 1: failed.
#
# Environment:
#   TUX_EXTRA_THEMES  folder of extra <id>/ themes, used as user themes in this run
#   TUX_BG            desktop colour, default #3a4150
#   TUX_KEEP          set to keep the temp folder (printed at the end) with shell.log and daemon.log
#   TUX_SCENARIO      fake_daemon.py scenario, default shots
#   TUX_EXTENSION     extension folder to test instead of extension/tuxistant@buengenio
#   TUX_SCALE         integer UI scale of the virtual monitor, default 1
#
# Safe next to a running desktop and next to other copies of itself: every run has its own runtime,
# config, data and cache folders, its own private session bus, and a private empty *system* bus, so
# this shell can never reach logind and touch the real session. Only its own children are killed.
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

    python3 -B "$here/fake_daemon.py" --theme "$theme" --out "$out" --scenario "$scenario" \
        >"$work/daemon.log" 2>&1 &
    daemon=$!
    for _ in $(seq 200); do
        grep -q 'ready for the shell' "$work/daemon.log" && break
        kill -0 "$daemon" 2>/dev/null || break
        sleep 0.05
    done

    if kill -0 "$daemon" 2>/dev/null; then
        gnome-shell --headless --wayland --no-x11 --wayland-display "tux-$$" \
            --virtual-monitor 1600x900 >"$work/shell.log" 2>&1 &
        shell=$!
    fi
    wait "$daemon"
    status=$?
    daemon=''
    exit $status
fi

if [ $# -ne 2 ]; then
    sed -n '2,9p' "$0" | cut -c3-
    exit 1
fi
theme=$1
out=$(realpath -m "$2")
mkdir -p "$out"
scenario=${TUX_SCENARIO:-shots}
bg=${TUX_BG:-#3a4150}

# Always /tmp: socket paths have a 108 byte limit that a long TMPDIR would break.
work=$(mktemp -d /tmp/tux-shot.XXXXXX)
mkdir -m 700 "$work/run"
mkdir -p "$work/home" "$work/cache" "$work/state" "$work/config/glib-2.0/settings" \
    "$work/data/gnome-shell/extensions" "$work/data/tuxistant/themes" "$work/empty"
cp -r "${TUX_EXTENSION:-$root/extension/tuxistant@buengenio}" "$here/tuxistant-shot@test" \
    "$work/data/gnome-shell/extensions/"
# git ignores the compiled schema, so a fresh clone has none: compile it in the copy, as install.sh
# does, or the extension can't load its settings.
for schemas in "$work"/data/gnome-shell/extensions/*/schemas; do
    [ -d "$schemas" ] && glib-compile-schemas "$schemas"
done
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
primary-color='$bg'

[org/gnome/desktop/interface]
enable-animations=true
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

# timeout(1) runs everything below in its own process group and forwards signals to it. Its one
# PID is all this script has to look after; the inner half stops its own children.
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

timeout_s=${TUX_TIMEOUT:-150}
env -i PATH="$PATH" HOME="$work/home" USER="${USER:-tux}" LOGNAME="${LOGNAME:-tux}" LANG=C.UTF-8 \
    XDG_RUNTIME_DIR="$work/run" XDG_CONFIG_HOME="$work/config" XDG_DATA_HOME="$work/data" \
    XDG_CACHE_HOME="$work/cache" XDG_STATE_HOME="$work/state" GSETTINGS_BACKEND=keyfile \
    XDG_CURRENT_DESKTOP=GNOME XDG_SESSION_TYPE=wayland PYTHONDONTWRITEBYTECODE=1 \
    TUX_PROBE_JS="${TUX_PROBE_JS:-}" \
    timeout -k 8 "$timeout_s" dbus-run-session --config-file="$work/bus.conf" -- \
    "$here/shot.sh" --inner "$work" "$theme" "$out" "$scenario" &
outer=$!
wait "$outer"
status=$?
outer=''

[ -s "$work/daemon.log" ] && grep -v 'ready for the shell' "$work/daemon.log" >&2

# Cogl reports shader problems as multi-line warnings, so keep some context after those.
errors=$( { grep -E -A14 'Shader compilation failed|Failed to link GLSL|shader.*(error|failed)' "$work/shell.log"
    grep -E 'Gjs-CRITICAL|JS ERROR|JS WARNING|SyntaxError|TypeError|ReferenceError|[Ee]xtension .*(error|Error)|[Tt]uxistant' "$work/shell.log"
} 2>/dev/null )
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
if [ "$scenario" = shots ]; then
    echo "wrote $out/listening.png $out/thinking.png $out/speaking.png"
fi
exit 0
