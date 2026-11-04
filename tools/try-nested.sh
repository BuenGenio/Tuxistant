#!/bin/bash
# Try Tuxistant in a windowed GNOME Shell: no logout, and a crash only closes that window.
# Needs mutter-dev-bin. The daemon uses your real config, mic and speakers on a private bus.
set -u
root=$(cd "$(dirname "$0")/.." && pwd -P)
uuid=tuxistant@buengenio
logs=${XDG_CACHE_HOME:-$HOME/.cache}/tuxistant
bin=$HOME/.local/bin/tuxistant

if [ "${1:-}" = --inner ]; then
    work=$2
    "$bin" daemon >"$logs/try-daemon.log" 2>&1 &
    daemon=$!
    XDG_CONFIG_HOME=$work/config XDG_DATA_HOME=$work/data XDG_CACHE_HOME=$work/cache \
    GSETTINGS_BACKEND=keyfile XDG_CURRENT_DESKTOP=GNOME XDG_SESSION_TYPE=wayland \
        gnome-shell --devkit --wayland --no-x11 --wayland-display tux-try >"$logs/try-shell.log" 2>&1
    status=$?
    kill "$daemon" 2>/dev/null
    exit "$status"
fi

[ -x /usr/libexec/mutter-devkit ] || { echo "install mutter-dev-bin first" >&2; exit 1; }
[ -x "$bin" ] || { echo "run ./install.sh first" >&2; exit 1; }
work=$(mktemp -d /tmp/tux-try.XXXXXX)
trap 'rm -rf "$work"' EXIT
mkdir -p "$logs" "$work/config/glib-2.0/settings" "$work/data/gnome-shell/extensions" "$work/cache"
cp -R "$root/extension/$uuid" "$work/data/gnome-shell/extensions/"
glib-compile-schemas "$work/data/gnome-shell/extensions/$uuid/schemas"
cat >"$work/config/glib-2.0/settings/keyfile" <<KEYFILE
[org/gnome/shell]
enabled-extensions=['$uuid']
disable-user-extensions=false
welcome-dialog-last-shown-version='999'
KEYFILE
dbus-run-session -- "$0" --inner "$work"
