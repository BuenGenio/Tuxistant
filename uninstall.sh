#!/bin/sh
# Reverses install.sh, including the Fn helper when it is installed. Your settings, API key,
# history and themes stay unless you pass --purge. Same environment as install.sh.
set -eu

PROJECT=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd -P)
# shellcheck source=packaging/paths.sh
. "$PROJECT/packaging/paths.sh"

usage() {
    echo "usage: uninstall.sh [--purge]"
}

remove_fn_helper() {
    can_touch_system || return 0
    [ -e "$HELPER_UNIT" ] || [ -e "$HELPER_DIR/tuxistant-fn" ] || return 0
    dry_run || sudo systemctl disable --now tuxistant-fn.service || true
    as_root rm -f "$HELPER_UNIT" "$HELPER_DIR/tuxistant-fn"
    as_root rmdir "$HELPER_DIR" 2>/dev/null || true
    dry_run || sudo systemctl daemon-reload
}

purge=0
for arg in "$@"; do
    case $arg in
        --purge) purge=1 ;;
        -h | --help) usage; exit 0 ;;
        *) usage >&2; exit 2 ;;
    esac
done

refuse_root
dry_run || systemctl --user disable --now tuxistant.service 2>/dev/null || true
rm -f "$WRAPPER" "$USER_UNIT" "$DBUS_SERVICE"
rm -rf "$EXTENSION_DIR"
dry_run || systemctl --user daemon-reload || warn "systemctl --user daemon-reload failed"
remove_fn_helper

echo "Tuxistant is uninstalled. The running shell keeps the extension until you log out."
if [ "$purge" = 1 ]; then
    rm -rf "$CONFIG_DIR" "$USER_DATA_DIR"
    echo "Removed your settings, API key, history and themes."
else
    echo "Kept $CONFIG_DIR and $USER_DATA_DIR (run with --purge to remove them)."
fi
