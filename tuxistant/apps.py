"""Opening an installed app or a page of GNOME Settings. Only a desktop file from Gio's own list is
ever started, with no files, addresses or arguments, so nothing the model writes reaches a command
line. Names are matched here, not by the model. App names are other people's text (a browser's
'install as app' uses the page title), so every reply naming one is said by the app, never
returned to the model.
"""
import difflib
import hashlib
import os
import re
import secrets
import shutil
import subprocess
import threading
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from .files import _tilde
from .gate import APP_SAYS, Ask, Speak, shown

OPEN_TIMEOUT_S = 10
MAX_SAID = 200          # characters of the request looked at
MAX_NAME = 60           # an app's name as said or shown
MAX_COMMAND = 300       # the command line on a card
MAX_CHOICES = 3
CLOSE_ENOUGH = 0.85     # difflib ratio for a misheard name
AGAIN_S = 5             # a 4B often sends the same call twice in one reply
SETTINGS_ID = "org.gnome.Settings.desktop"
GROUP = "Desktop Entry"
FILLER = {"the", "my", "a", "an", "for", "of", "in"}    # stripped from the front only
VERBS = {"open", "launch", "start", "show", "me"}       # the 4B often passes the whole request
TAIL = {"app", "application", "program"}                # stripped from the end, if more is left
SETTINGS_WORDS = {"setting", "preference", "option", "page", "panel"}   # as _words() leaves them
SETTINGS_PHRASES = ("control center", "control centre", "control panel")
KINDS = {"flatpak": "Flatpak", "snap": "Snap", "system": "system", "home": "home",
         "other": "other"}
PACKAGED = {"system", "Flatpak", "Snap"}
KIND_SAID = {"Flatpak": "a Flatpak", "Snap": "a Snap", "system": "one from the system",
             "home": "one added in your home folder", "other": "one from another folder"}
# An Exec basename that says nothing about the app.
LAUNCHERS = {"flatpak", "snap", "env", "sh", "bash", "python", "python3", "java", "wine", "gjs",
             "electron", "apprun", "gio", "gtk-launch", "pkexec", "sudo"}
# Keys in _words() form. "mail app" loses its TAIL before it gets here, so the bare forms count.
ALIASES = {"browser": "https", "web browser": "https", "internet": "https", "mail": "mailto",
           "email": "mailto", "e mail": "mailto", "mail app": "mailto", "email app": "mailto"}

NO_NAME = "Say which app or Settings page to open."
NOT_FOUND = ("I can't find an app called {name} on this computer. If it's a file or a folder, "
             "I can look for it.")
NO_LIST = "I can't list the apps on this computer."
WHICH = "Which one: {names}?"
MORE = "Several apps match that, such as {names}. Which one?"
TWINS = "There's more than one {name}: {kinds}. Say {choices}."
OPENING = "Opening {name}."
OPENING_PAGE = "Opening {title} in Settings."
OPENING_SETTINGS = "Opening Settings."
WONT_OPEN = "{name} wouldn't open."
NO_SETTINGS = "GNOME Settings isn't installed on this computer."
NO_PAGE_HERE = "Settings on this computer has no {title} page."
NO_SUCH_PAGE = ("Settings has no page called {name}. It has pages such as Bluetooth, display, "
                "sound, Wi-Fi, network and power.")
CHANGED = "That app's shortcut changed after the card was shown, so I've left it alone."
UNREADABLE = "I can't read that app's shortcut, so I've left it alone."
CARD_WHY = {"home": "It was added in your home folder, not installed as a package, so it can run "
                    "any command.",
            "other": "It isn't from an installed package, so it can run any command."}
OVER_DBUS = "It may be started over D-Bus by a service of its own, not by this command."
IN_TERMINAL = "It runs in a terminal."


def _single(word: str) -> str:
    return word[:-1] if len(word) > 3 and word.endswith("s") and not word.endswith("ss") else word


def _without_filler(words: list[str]) -> list[str]:
    while words and (words[0] in FILLER or words[0] in VERBS):
        words = words[1:]
    return words


def _words(text) -> str:
    """One form for what was said, the table's words and every app's names, so plurals, accents
    and "Wi-Fi" against "wifi" meet."""
    text = unicodedata.normalize("NFKD", str(text)[:MAX_SAID]).casefold()
    text = "".join(c for c in text if not unicodedata.combining(c)).replace("&", " and ")
    return " ".join(_without_filler([_single(w) for w in re.findall(r"[^\W_]+", text)]))


def _squash(text: str) -> str:
    return text.replace(" ", "")


def _said(text) -> str:
    """Other people's text, fit to say and show: no chat-template delimiters, no hidden lines."""
    return shown(" ".join(str(text).replace("<", " ").replace(">", " ").split()), MAX_NAME)


@dataclass(frozen=True)
class Panel:
    desktop: str
    title: str
    words: frozenset


def _panel_row(desktop: str, title: str, *words: str) -> Panel:
    return Panel(desktop, title, frozenset(_words(word) for word in words))


# Owned by the daemon: GNOME Settings 50 has one NoDisplay desktop file per page. "clock" is left
# out, as it belongs to the Clocks app.
PANELS = (
    _panel_row("gnome-bluetooth-panel.desktop", "Bluetooth", "bluetooth"),
    _panel_row("gnome-display-panel.desktop", "Displays", "display", "displays", "screen",
               "monitor", "resolution", "night light", "scaling"),
    _panel_row("gnome-sound-panel.desktop", "Sound", "sound", "audio", "volume", "speaker",
               "microphone", "headphones", "sound output", "sound input"),
    _panel_row("gnome-wifi-panel.desktop", "Wi-Fi", "wifi", "wi fi", "wireless", "hotspot"),
    _panel_row("gnome-network-panel.desktop", "Network", "network", "ethernet", "wired", "vpn",
               "proxy"),
    _panel_row("gnome-background-panel.desktop", "Appearance", "appearance", "background",
               "wallpaper", "dark mode", "dark style", "theme", "accent colour", "accent color"),
    _panel_row("gnome-notifications-panel.desktop", "Notifications", "notifications",
               "do not disturb"),
    _panel_row("gnome-power-panel.desktop", "Power", "power", "battery", "sleep", "suspend",
               "power saving", "energy"),
    _panel_row("gnome-keyboard-panel.desktop", "Keyboard", "keyboard", "keyboard shortcuts",
               "shortcuts", "input sources", "typing"),
    _panel_row("gnome-mouse-panel.desktop", "Mouse and Touchpad", "mouse", "touchpad", "trackpad",
               "mouse and touchpad"),
    _panel_row("gnome-printers-panel.desktop", "Printers", "printer", "printers", "printing"),
    _panel_row("gnome-online-accounts-panel.desktop", "Online Accounts", "online accounts",
               "accounts", "google account", "microsoft account", "nextcloud"),
    _panel_row("gnome-privacy-panel.desktop", "Privacy and Security", "privacy", "security",
               "privacy and security", "screen lock", "lock screen", "location"),
    _panel_row("gnome-sharing-panel.desktop", "Sharing", "sharing", "file sharing",
               "media sharing"),
    _panel_row("gnome-multitasking-panel.desktop", "Multitasking", "multitasking", "workspaces",
               "hot corner"),
    _panel_row("gnome-search-panel.desktop", "Search", "search"),
    _panel_row("gnome-applications-panel.desktop", "Apps", "apps", "applications", "default apps",
               "default applications", "app permissions"),
    _panel_row("gnome-universal-access-panel.desktop", "Accessibility", "accessibility",
               "universal access", "large text", "screen reader", "zoom"),
    _panel_row("gnome-color-panel.desktop", "Colour", "colour", "color", "colour management",
               "color management", "colour profile", "color profile"),
    _panel_row("gnome-wacom-panel.desktop", "Graphics Tablet", "graphics tablet",
               "drawing tablet", "tablet", "wacom", "stylus"),
    _panel_row("gnome-wwan-panel.desktop", "Mobile Network", "mobile network", "mobile data",
               "mobile broadband", "sim"),
    _panel_row("gnome-wellbeing-panel.desktop", "Wellbeing", "wellbeing", "screen time",
               "break reminders"),
    _panel_row("gnome-system-panel.desktop", "System", "system", "remote desktop",
               "remote login"),
    _panel_row("gnome-about-panel.desktop", "About", "about", "about this computer",
               "system details"),
    _panel_row("gnome-datetime-panel.desktop", "Date and Time", "date and time", "date", "time",
               "time zone", "timezone"),
    _panel_row("gnome-region-panel.desktop", "Region and Language", "region", "language",
               "region and language", "formats"),
    _panel_row("gnome-users-panel.desktop", "Users", "users", "user accounts", "password",
               "fingerprint", "login", "automatic login"),
    _panel_row("gnome-ubuntu-panel.desktop", "Ubuntu Desktop", "ubuntu desktop", "ubuntu", "dock"),
)


@dataclass(frozen=True)
class App:
    id: str
    name: str               # as said and shown: _said(get_name())
    path: str               # absolute, from Gio's own list
    kind: str               # "system", "Flatpak", "Snap", "home" or "other"
    shown: bool             # NoDisplay entries are reached only by id: Settings pages, a default
    names: frozenset        # the names shown, translated or not; these five in _words() form
    generics: frozenset
    keywords: frozenset
    # Matched only exactly and only when no name fits: every Steam game's shortcut runs "steam",
    # and every Chrome web app runs "google-chrome" from an id starting "chrome-".
    id_name: str = ""
    program: str = ""


def _gio():
    try:
        import gi
        gi.require_version("Gio", "2.0")
        from gi.repository import Gio
    except (ImportError, ValueError):
        raise RuntimeError(NO_LIST) from None
    return Gio


def _glib():
    _gio()
    from gi.repository import GLib
    return GLib


def _installed() -> list[App]:
    return [_app(info) for info in _gio().AppInfo.get_all()
            if (getattr(info, "get_filename", lambda: None)() or "").startswith("/")]


def _default_for(scheme: str) -> str | None:
    info = _gio().AppInfo.get_default_for_uri_scheme(scheme)
    return info.get_id() if info else None


def _id_word(desktop_id: str) -> str:
    """org.mozilla.firefox is firefox, and a Snap's thunderbird_thunderbird is thunderbird."""
    return desktop_id.removesuffix(".desktop").partition("_")[0].rpartition(".")[2]


def _forms(values) -> frozenset:
    return frozenset(form for form in map(_words, filter(None, values)) if form)


def _app(info) -> App:
    path = info.get_filename()
    desktop_id = info.get_id() or Path(path).name
    names = [info.get_name(), info.get_string("Name"), info.get_string("X-GNOME-FullName"),
             info.get_locale_string("X-GNOME-FullName")]
    program = Path(info.get_executable() or "").name
    keywords = [*(info.get_keywords() or ()), *(info.get_string("Keywords") or "").split(";")]
    return App(desktop_id, _said(info.get_name() or desktop_id), path, _kind(path),
               bool(info.should_show()), _forms(names),
               _forms([info.get_generic_name(), info.get_string("GenericName")]),
               _forms(keywords), _words(_id_word(desktop_id)),
               "" if program.casefold() in LAUNCHERS else _words(program))


def _kind(path: str) -> str:
    return _where(os.path.realpath(path))


def _where(real: str) -> str:
    """Judged by where the file really is, as Gio runs whatever a link points at."""
    home = os.path.realpath(Path.home())
    data = os.path.realpath(os.environ.get("XDG_DATA_HOME") or f"{home}/.local/share")
    if real.startswith(("/var/lib/flatpak/", f"{data}/flatpak/app/")):
        return "Flatpak"
    if real.startswith(("/var/lib/snapd/", "/snap/")):
        return "Snap"
    if real.startswith(("/usr/", "/nix/store/")):
        return "system"
    if real.startswith((f"{home}/", f"{data}/")):
        return "home"
    return "other"      # /opt, /etc/xdg, Guix, an extra Flatpak installation: no package vouches


def _without_tail(query: str) -> str:
    words = query.split()
    return " ".join(words[:-1]) if len(words) > 1 and words[-1] in TAIL else query


def _settings_asked(query: str) -> tuple[str, bool]:
    padded, asked = f" {query} ", False
    for phrase in (*SETTINGS_PHRASES, *SETTINGS_WORDS):
        if f" {phrase} " in padded:
            padded, asked = padded.replace(f" {phrase} ", " "), True
    return " ".join(_without_filler(padded.split())), asked


def _kind_asked(query: str) -> tuple[str, str | None]:
    words = query.split()
    if len(words) > 1 and words[0] in KINDS:
        return " ".join(words[1:]), KINDS[words[0]]
    if len(words) > 1 and words[-1] in KINDS:
        return " ".join(words[:-1]), KINDS[words[-1]]
    return query, None


def _panel(query: str, entries: list[App], loose: bool) -> Panel | None:
    squashed = _squash(query)
    for panel in PANELS:
        if squashed in {_squash(word) for word in panel.words}:
            return panel
    by_id = {entry.id: entry for entry in entries}
    for panel in PANELS:    # a page's own name, translated or not
        if panel.desktop in by_id and query in by_id[panel.desktop].names:
            return panel
    if not loose:
        return None
    inside = [(len(word), panel) for panel in PANELS for word in panel.words
              if f" {word} " in f" {query} "]
    return max(inside, key=lambda found: found[0], default=(0, None))[1]


def _close(squashed: str, entries: list[App]) -> tuple[int | None, list[App]]:
    if len(squashed) < 4:
        return None, []

    def score(entry: App) -> float:
        forms = {_squash(name) for name in entry.names}
        forms |= {word for name in entry.names for word in name.split()}
        return max((difflib.SequenceMatcher(None, squashed, form).ratio() for form in forms),
                   default=0.0)

    scored = [(score(entry), entry) for entry in entries]
    found = [entry for value, entry in sorted(scored, key=lambda pair: -pair[0])
             if value >= CLOSE_ENOUGH]
    return (6, found) if found else (None, [])


def _is(query: str, forms) -> bool:
    return query in forms or _squash(query) in {_squash(form) for form in forms}


def _matching(query: str, entries: list[App]) -> tuple[int | None, list[App]]:
    query = _without_tail(query)
    if query in ALIASES and (wanted := _default_for(ALIASES[query])):
        chosen = [entry for entry in entries if entry.id == wanted]
        if chosen:      # even NoDisplay: the user picked it as their default
            return 1, chosen[:1]
    tiers = (lambda e: _is(query, e.names),
             lambda e: query in e.generics,
             lambda e: any(f" {query} " in f" {name} " for name in e.names),
             lambda e: _is(query, {e.id_name} - {""}),
             lambda e: _is(query, {e.program} - {""}),
             lambda e: query in e.keywords)
    shown_ones = [entry for entry in entries if entry.shown]
    for tier, test in enumerate(tiers):
        if found := [entry for entry in shown_ones if test(entry)]:
            return tier, found
    return _close(_squash(query), shown_ones)


def _matching_kind(query: str, entries: list[App]) -> tuple[int | None, list[App]]:
    """"Flatpak Firefox" looks among the Flatpaks first, and at everything if none fits."""
    rest, kind = _kind_asked(query)
    if kind:
        tier, found = _matching(rest, [entry for entry in entries if entry.kind == kind])
        if found:
            return tier, found
    return _matching(query, entries)


def _joined(items: list[str], last: str) -> str:
    return items[0] if len(items) == 1 else f"{', '.join(items[:-1])} {last} {items[-1]}"


def _say(session, config, text: str) -> dict:
    session.after.append(Speak(text, config["voice"]))
    return {APP_SAYS: True}


def _twins(group: list[App], config, session):
    kinds = list(dict.fromkeys(app.kind for app in group))
    if len(kinds) == 1:
        return _start(group[0], OPENING.format(name=group[0].name), config, session)
    name = group[0].name
    return _say(session, config, TWINS.format(
        name=name, kinds=_joined([KIND_SAID[kind] for kind in kinds], "and"),
        choices=_joined([f"{kind} {name}" for kind in kinds], "or")))


def _open_found(found: list[App], config, session):
    groups: dict[str, list[App]] = {}
    for app in found:
        groups.setdefault(app.name, []).append(app)
    names = list(groups)
    if len(names) == 1:
        return _twins(groups[names[0]], config, session)
    if len(names) <= MAX_CHOICES:
        return _say(session, config, WHICH.format(names=_joined(names, "or")))
    return _say(session, config, MORE.format(names=_joined(names[:MAX_CHOICES], "and")))


def _open_panel(panel: Panel, entries: list[App], config, session):
    by_id = {entry.id: entry for entry in entries}
    if panel.desktop in by_id:
        return _start(by_id[panel.desktop], OPENING_PAGE.format(title=panel.title), config,
                      session)
    if SETTINGS_ID not in by_id:
        return _say(session, config, NO_SETTINGS)
    return _say(session, config, NO_PAGE_HERE.format(title=panel.title))


def _open_by_id(desktop_id: str, text: str, entries: list[App], config, session):
    for entry in entries:
        if entry.id == desktop_id:
            return _start(entry, text, config, session)
    return _say(session, config, NO_SETTINGS)


def _start(app: App, text: str, config, session) -> dict | Ask:
    if app.kind in PACKAGED:
        return _launch_and_say(app, text, config, session)
    # The card is made from the very bytes whose digest is checked after the click.
    data = _read(app.path)
    keys = _keyfile(data) if data is not None else None
    if keys is None:
        return _say(session, config, UNREADABLE)
    digest = hashlib.sha256(data).hexdigest()

    def run() -> dict:
        if _digest(app.path) != digest:
            return _say(session, config, CHANGED)
        return _launch_and_say(app, text, config, session)

    return Ask(f"Open {app.name}?", _card(app, keys), run)


def _card(app: App, keys) -> str:
    lines = [CARD_WHY[app.kind]]
    if command := _value(keys, "Exec"):
        lines.append(f"Runs: {shown(command, MAX_COMMAND)}")
    if _flag(keys, "DBusActivatable"):
        lines.append(OVER_DBUS)
    if _flag(keys, "Terminal"):
        lines.append(IN_TERMINAL)
    if folder := _value(keys, "Path"):
        lines.append(f"In folder: {shown(folder)}")
    lines.append(f"File: {shown(_tilde(Path(app.path)))}")
    return "\n".join(lines)


def _keyfile(data: bytes):
    GLib = _glib()
    keys = GLib.KeyFile()
    try:
        keys.load_from_bytes(GLib.Bytes.new(data), GLib.KeyFileFlags.NONE)
    except GLib.Error:
        return None
    return keys if keys.has_group(GROUP) else None


def _value(keys, key: str) -> str:
    try:
        return keys.get_string(GROUP, key) or ""
    except _glib().Error:
        return ""


def _flag(keys, key: str) -> bool:
    try:
        return bool(keys.get_boolean(GROUP, key))
    except _glib().Error:
        return False


def _launch_and_say(app: App, text: str, config, session) -> dict:
    if not _again(app.id):
        if not _launch(app.path):
            return _say(session, config, WONT_OPEN.format(name=app.name))
        _remember(app.id)
    return _say(session, config, text)


# Only launches that worked, so a duplicate call after a failure tries again rather than
# claiming the app is opening.
_launched: dict[str, float] = {}
_launched_lock = threading.Lock()


def _again(desktop_id: str) -> bool:
    with _launched_lock:
        return time.monotonic() - _launched.get(desktop_id, -AGAIN_S) < AGAIN_S


def _remember(desktop_id: str) -> None:
    with _launched_lock:
        _launched[desktop_id] = time.monotonic()


def _read(path: str) -> bytes | None:
    try:
        return Path(path).read_bytes()
    except OSError:
        return None


def _digest(path: str) -> str:
    data = _read(path)
    return "" if data is None else hashlib.sha256(data).hexdigest()


def _scope(desktop_id: str) -> list[str]:
    unit = re.sub(r"[^A-Za-z0-9_.]", "_", desktop_id.removesuffix(".desktop"))[:64]
    return ["systemd-run", "--user", "--scope", "--collect", "--quiet", "--slice=app.slice",
            f"--unit=app-tuxistant-{unit}-{secrets.token_hex(4)}.scope", "--"]


def _launch(path: str) -> bool:
    """In a scope of its own: a child of the daemon would live in tuxistant.service, under its
    1 GB memory limit, and be stopped by every restart. No pipes: the app inherits them and stays
    open."""
    if not (os.path.isabs(path) and path.endswith(".desktop")):
        return False
    command = ["gio", "launch", path]
    if shutil.which("systemd-run"):
        command = [*_scope(Path(path).name), *command]
    try:
        return subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, timeout=OPEN_TIMEOUT_S).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def open_app(args: dict, config, session):
    query, settings = _settings_asked(_without_tail(_words(args.get("name") or "")))
    if not query and not settings:
        raise ValueError(NO_NAME)
    entries = _installed()
    if not query or (settings and query == "gnome"):     # "GNOME Settings", "GNOME Control Center"
        return _open_by_id(SETTINGS_ID, OPENING_SETTINGS, entries, config, session)
    # An app named exactly this wins ("Terminal", "Clocks"), then a page named exactly this
    # ("Bluetooth" is the page, not Blueman), then looser app matches. "Firefox settings" opens
    # Firefox, as Settings has no page for it.
    tier, found = _matching_kind(query, entries)
    panel = _panel(query, entries, loose=settings)
    if panel and (settings or tier != 0):
        return _open_panel(panel, entries, config, session)
    if found:
        return _open_found(found, config, session)
    if settings:
        return _say(session, config, NO_SUCH_PAGE.format(name=_said(query)))
    # Only the user's words, so the model can still try file_find for a folder.
    raise ValueError(NOT_FOUND.format(name=_said(query)))


HANDLERS = {"app.open": open_app}
