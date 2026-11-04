#!/bin/sh
# Installs Tuxistant for the current user, including the Fn key helper, the only part that
# needs sudo. --no-fn-helper skips it. Safe to run again, for example after pulling a new version.
# Cloud mode needs the Claude SDK: it asks at a terminal, and --cloud-sdk adds it without asking.
#
# XDG_DATA_HOME, XDG_CONFIG_HOME and XDG_BIN_HOME pick the destinations. DESTDIR stages every
# file under a prefix. TUXISTANT_INSTALL_DRYRUN=1 skips ssh, sudo, systemctl and pip.
set -eu

PROJECT=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd -P)
# shellcheck source=packaging/paths.sh
. "$PROJECT/packaging/paths.sh"

usage() {
    echo "usage: install.sh [--no-fn-helper] [--cloud-sdk]"
}

install_wrapper() {
    install -d "$(dirname -- "$WRAPPER")"
    quoted=$(printf %s "$PROJECT" | sed "s/'/'\\\\''/g")
    # -P: without it -m puts the current directory first, ahead of PYTHONPATH.
    cat > "$WRAPPER.tmp" <<EOF
#!/bin/sh
PYTHONPATH='$quoted'\${PYTHONPATH:+:\$PYTHONPATH} exec /usr/bin/python3 -P -m tuxistant "\$@"
EOF
    chmod 0755 "$WRAPPER.tmp"
    mv -f "$WRAPPER.tmp" "$WRAPPER"
}

install_extension() {
    source_dir=$PROJECT/extension/$UUID
    [ -f "$source_dir/metadata.json" ] || die "no extension at $source_dir"
    rm -rf "$EXTENSION_DIR"
    install -d "$(dirname -- "$EXTENSION_DIR")"
    cp -R "$source_dir" "$EXTENSION_DIR"
    if [ -d "$EXTENSION_DIR/schemas" ]; then
        glib-compile-schemas "$EXTENSION_DIR/schemas"
    fi
}

install_units() {
    install -d "$(dirname -- "$USER_UNIT")" "$(dirname -- "$DBUS_SERVICE")"
    if [ "$BIN_HOME" = "$HOME/.local/bin" ]; then
        install -m 0644 "$PROJECT/packaging/tuxistant.service" "$USER_UNIT"
    else
        sed "s|%h/.local/bin|$BIN_HOME|" "$PROJECT/packaging/tuxistant.service" > "$USER_UNIT"
        chmod 0644 "$USER_UNIT"
    fi
    install -m 0644 "$PROJECT/packaging/org.tuxistant.Daemon.service" "$DBUS_SERVICE"
}

config_has_key() {
    [ -f "$1" ] && /usr/bin/python3 -P -c '
import json, sys
with open(sys.argv[1]) as f:
    sys.exit(0 if json.load(f).get("api_key") else 1)' "$1" 2>/dev/null
}

# The key travels through stdin only, so it never shows up in a process listing.
write_config() {
    /usr/bin/python3 -P -c '
import json, os, sys
path = sys.argv[1]
key = sys.stdin.read().strip()
config = {}
if os.path.exists(path):
    with open(path) as f:
        config = json.load(f)
if key:
    config["api_key"] = key
tmp = path + ".tmp"
with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
    json.dump(config, f, indent=2)
os.replace(tmp, path)' "$1"
}

install_config() {
    config=$CONFIG_DIR/config.json
    install -d -m 0700 "$CONFIG_DIR"
    if config_has_key "$config"; then
        chmod 0600 "$config"
        return
    fi
    key=
    # TUXISTANT_KEY_COMMAND prints the API key, for example by asking the server over SSH. The key
    # goes straight into the config, and a script never has to hold it.
    if ! dry_run && [ -n "${TUXISTANT_KEY_COMMAND:-}" ]; then
        key=$(sh -c "$TUXISTANT_KEY_COMMAND" 2>/dev/null) || key=
        [ -n "$key" ] || warn "could not fetch the API key; set it in Settings"
    fi
    printf %s "$key" | write_config "$config" 2>/dev/null ||
        warn "could not update $config, left as it was; is it valid JSON?"
    key=
}

check_dependencies() {
    # -P, as everywhere here: -c would look in the current folder first. Not -I: the modules can
    # be in the user's site folder.
    /usr/bin/python3 -P -c 'import dbus_fast, websockets, yaml, gi' 2>/dev/null ||
        warn "missing Python modules: the daemon needs dbus_fast, websockets, PyYAML and gi"
}

reload_user_manager() {
    dry_run || systemctl --user daemon-reload || warn "systemctl --user daemon-reload failed"
}

cloud_sdk_works() {
    PYTHONPATH=$1 /usr/bin/python3 -P -c 'import anthropic' 2>/dev/null
}

wants_cloud_sdk() {
    [ "$cloud_sdk" = 0 ] || return 0
    ! dry_run || return 1
    if [ ! -t 0 ]; then
        echo "Cloud mode needs the Claude SDK: ./install.sh --cloud-sdk adds it."
        return 1
    fi
    printf 'Add the Claude SDK for the optional cloud mode? It takes about 50 MB. [y/N] '
    read -r answer || answer=
    case $answer in
        [yY]*) ;;
        *) return 1 ;;
    esac
}

# Nothing but the opt-in cloud mode needs this, so a failure here never fails the install. It has
# a folder of its own, which the planner adds to sys.path when it is needed, as pip must leave the
# system Python alone. This Python, not the first on PATH: the SDK has compiled parts.
install_cloud_sdk() {
    vendor=$USER_DATA_DIR/vendor
    ! cloud_sdk_works "$vendor" || return 0
    wants_cloud_sdk || return 0
    if dry_run; then
        echo "Dry run: skipping the Claude SDK."
        return
    fi
    /usr/bin/python3 -P -m pip install --quiet --upgrade --target "$vendor" anthropic ||
        warn "could not add the Claude SDK; ./install.sh --cloud-sdk tries again"
}

install_fn_helper() {
    if ! can_touch_system; then
        echo "Dry run: skipping $HELPER_DIR and $HELPER_UNIT (set DESTDIR to stage them)."
        return
    fi
    as_root install -d -m 0755 "$HELPER_DIR" "$(dirname -- "$HELPER_UNIT")"
    as_root install -m 0755 "$PROJECT/helper/tuxistant-fn" "$HELPER_DIR/tuxistant-fn"
    as_root install -m 0644 "$PROJECT/helper/tuxistant-fn.service" "$HELPER_UNIT"
    dry_run && return
    sudo systemctl daemon-reload
    sudo systemctl enable tuxistant-fn.service
    sudo systemctl restart tuxistant-fn.service
    # Holding Fn reaches the daemon only if it is already running, so start it at login.
    systemctl --user enable tuxistant.service || warn "could not enable tuxistant.service"
}

fn_helper=1
cloud_sdk=0
for arg in "$@"; do
    case $arg in
        --fn-helper) fn_helper=1 ;;
        --no-fn-helper) fn_helper=0 ;;
        --cloud-sdk) cloud_sdk=1 ;;
        -h | --help) usage; exit 0 ;;
        *) usage >&2; exit 2 ;;
    esac
done

refuse_root
check_dependencies
install_wrapper
install_extension
install_units
install_config
reload_user_manager
install_cloud_sdk
[ "$fn_helper" = 0 ] || install_fn_helper

echo "Tuxistant is installed. What remains:"
echo "  1. Log out and back in once (Wayland only loads a new extension at login)."
echo "  2. gnome-extensions enable $UUID"
if [ "$fn_helper" = 0 ]; then
    echo "Holding Fn to talk needs the helper: run ./install.sh without --no-fn-helper."
fi
case :$PATH: in
    *:"$BIN_HOME":*) ;;
    *) echo "Note: $BIN_HOME is not on your PATH, so the tuxistant command needs its full path." ;;
esac
