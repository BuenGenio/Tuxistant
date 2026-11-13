# Sourced by install.sh and uninstall.sh. PROJECT must be set.
# shellcheck shell=sh disable=SC2034

DESTDIR=${DESTDIR:-}
DATA_HOME=${XDG_DATA_HOME:-$HOME/.local/share}
CONFIG_HOME=${XDG_CONFIG_HOME:-$HOME/.config}
STATE_HOME=${XDG_STATE_HOME:-$HOME/.local/state}
BIN_HOME=${XDG_BIN_HOME:-$HOME/.local/bin}

UUID=tuxistant@buengenio
WRAPPER=$DESTDIR$BIN_HOME/tuxistant
EXTENSION_DIR=$DESTDIR$DATA_HOME/gnome-shell/extensions/$UUID
USER_UNIT=$DESTDIR$DATA_HOME/systemd/user/tuxistant.service
DBUS_SERVICE=$DESTDIR$DATA_HOME/dbus-1/services/org.tuxistant.Daemon.service
CONFIG_DIR=$DESTDIR$CONFIG_HOME/tuxistant
USER_DATA_DIR=$DESTDIR$DATA_HOME/tuxistant
STATE_DIR=$DESTDIR$STATE_HOME/tuxistant  # each code skill's /state, see skillbox.py
HELPER_DIR=$DESTDIR/usr/local/lib/tuxistant
HELPER_UNIT=$DESTDIR/etc/systemd/system/tuxistant-fn.service

warn() { echo "tuxistant: $*" >&2; }
die() { warn "$*"; exit 1; }
dry_run() { [ "${TUXISTANT_INSTALL_DRYRUN:-}" = 1 ]; }

# A dry run may only touch system paths when they are staged under DESTDIR.
can_touch_system() { ! dry_run || [ -n "$DESTDIR" ]; }

as_root() {
    if dry_run; then "$@"; else sudo "$@"; fi
}

refuse_root() {
    [ "$(id -u)" != 0 ] || die "run this as your own user; it uses sudo itself where needed"
}
