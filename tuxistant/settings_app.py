"""Tuxistant Settings: a libadwaita window onto the daemon.

Everything goes through D-Bus and nothing blocks the UI. The one exception is the toggle
shortcut, which belongs to the Shell extension and lives in its GSettings schema.
"""
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

import gi

gi.require_version("Adw", "1")
gi.require_version("Gtk", "4.0")
from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk  # noqa: E402

from . import BUS_NAME, BUS_PATH, homeassistant, themes  # noqa: E402
from .config import DEFAULTS, clean_server  # noqa: E402
from .gate import MAX_TITLE_CHARS, printable  # noqa: E402
from .livedata import DEFAULT_SOURCE, SOURCES  # noqa: E402

APP_ID = "org.tuxistant.Settings"
EXTENSION_UUID = "tuxistant@buengenio"
SHELL_SCHEMA = "org.gnome.shell.extensions.tuxistant"
SHORTCUT_KEY = "toggle-shortcut"
PIPELINES = (
    ("tux-realtime", "Default pipeline"),
    ("tux-realtime-eou", "English only. Uses extra GPU memory"),
)
HISTORY_ROWS = 100
READ_TIMEOUT_MS = 5000
START_TIMEOUT_MS = 30000    # the first call may have to D-Bus activate the daemon
THEMES_TIMEOUT_MS = 30000   # a theme not seen before gets test-compiled first
PREVIEW_TIMEOUT_MS = 90000  # the daemon replies once the sample has been spoken, or has failed
PHONES_TIMEOUT_MS = 10000   # the daemon gives GSConnect 6 seconds to answer
SAVE_DELAY_MS = 400

RISK_CLASSES = {"low": "success", "medium": "warning", "high": "error"}
STATUS_WORDS = {"cancelled": "Cancelled", "error": "Ended with an error"}
PERMISSION_WORDS = {
    "shell": "run shell commands",
    "files_write": "change files",
    "files_read": "read files",
    "network": "use the network",
    "clipboard": "use the clipboard",
    "calendar_read": "read your calendars",
    "mail_read": "read your mail",
    "calendar_write": "add events to a calendar",
    "home_control": "control devices through Home Assistant",
    "home_read": "read the state of your home",
}
HOME_SERVER = "home"        # a network permission naming the server this install talks to
SHELL_ACTION = "shell.run"
KEY_SET = {"api_key": "api_key_set", "cloud_api_key": "cloud_key_set",   # never come back
           "tapo_password": "tapo_password_set", "ha_token": "ha_token_set"}
KEY_WORDS = {"tapo_password": ("Password is set", "No password set"),
             "ha_token": ("Token is set", "No token set")}
LIGHTS_NOTE = ("The TP-Link account your Tapo lights are set up with. The password never "
               "leaves this computer")
HOME_NOTE = ("Make a long-lived access token on your profile page in Home Assistant, under "
             "Security. Enter the address first: a new address needs its own token. Plain "
             "http is only used on your own network")
MAIL_NOTE = ("Answers come from the mail Thunderbird keeps on this computer. What a mail says "
             "goes to your own server to be read, and nowhere else")
AUTOMATIC = "Automatic"
PHONE_NOTE = "Used when you don't name a device"
CALENDARS_TIMEOUT_MS = 30000    # the daemon connects to each calendar to see if it can be changed
CALENDAR_NOTE = "Where new events go when you don't name a calendar"
NO_CALENDARS = "No calendar here can be changed. Add an account in GNOME Settings, Online Accounts"
# A timeout or an older daemon says nothing about accounts, so no advice to add one.
CALENDARS_FAILED = "Tuxistant couldn't list your calendars just now"
GONE_CALENDAR = "Not available: a calendar that is no longer here"
NO_PHONES = "GSConnect isn't running or has no devices"
CLOUD_HEADING = "Send complex requests to Anthropic?"
CLOUD_BODY = (
    "When a request needs several steps, your spoken request and the file names and command "
    "output from that task leave this computer and go to Anthropic.\n\n"
    "File contents go only when you click to approve each one.\n\n"
    "Ordinary single-step requests send nothing to Anthropic."
)
RAW_FIELDS = ("Command", "Arguments", "Result")     # shown as they are, so columns stay columns
CSS = """
.risk-badge {
  padding: 2px 10px;
  border-radius: 999px;
  font-size: 0.85em;
  font-weight: 600;
  background-color: color-mix(in srgb, currentColor 15%, transparent);
}
.raw .subtitle {
  font-family: monospace;
}
"""
_NOT_ANSWERING = {
    (GLib.quark_to_string(Gio.DBusError.quark()), code) for code in (
        Gio.DBusError.SERVICE_UNKNOWN, Gio.DBusError.NAME_HAS_NO_OWNER, Gio.DBusError.NO_REPLY,
        Gio.DBusError.DISCONNECTED, Gio.DBusError.TIMED_OUT, Gio.DBusError.TIMEOUT,
        Gio.DBusError.SPAWN_EXEC_FAILED, Gio.DBusError.SPAWN_CHILD_EXITED,
        Gio.DBusError.SPAWN_FAILED, Gio.DBusError.SPAWN_SERVICE_NOT_FOUND)
} | {
    (GLib.quark_to_string(Gio.io_error_quark()), code)
    for code in (Gio.IOErrorEnum.TIMED_OUT, Gio.IOErrorEnum.CLOSED)
}


def not_answering(error: GLib.Error) -> bool:
    """True when nothing answered, as opposed to the daemon refusing the request."""
    return (error.domain, error.code) in _NOT_ANSWERING


def error_text(message: str) -> str:
    """A GError message without its D-Bus error name."""
    return re.sub(r"^GDBus\.Error:[^ ]+ ?", "", message) or "The request failed"


def group_voices(voices: list[dict]) -> dict[str, list[dict]]:
    groups = {}
    for voice in voices:
        groups.setdefault(voice["accent"], []).append(voice)
    return groups


def voice_matches(voice: dict, query: str) -> bool:
    """Every word must match. Accent and gender match from the start, so "male" skips "Female"."""
    name, accent, gender = (voice[key].lower() for key in ("name", "accent", "gender"))
    return all(word in name or accent.startswith(word) or gender.startswith(word)
               for word in query.lower().split())


def shown(text: str) -> str:
    """Text from outside, as a label can show it. A label stops at a NUL without a word, and a
    lone surrogate, which JSON carries happily, raises and would take the whole page with it."""
    return printable(text, sys.maxsize)


def permission_words(permissions: dict) -> str:
    """A skill's permissions as one sentence. Unknown ones still show: hiding them would mislead."""
    known = [key for key in PERMISSION_WORDS if key in permissions]
    parts = []
    for key in known + [key for key in permissions if key not in PERMISSION_WORDS]:
        value = permissions[key]
        if not value:
            continue
        words = PERMISSION_WORDS.get(key) or "use " + key.replace("_", " ")
        if isinstance(value, str):
            value = [value]
        if isinstance(value, (list, tuple)):
            names = ["your own server" if key == "network" and name == HOME_SERVER else str(name)
                     for name in value]
            words += f" ({', '.join(names)})"
        parts.append(words)
    if not parts:
        return "Needs no special permissions"
    head = ", ".join(parts[:-1])
    return f"Can {head} and {parts[-1]}" if head else f"Can {parts[-1]}"


def risk_label(risk: str) -> str:
    return f"{risk.capitalize()} risk"


def format_time(ts: float, now: float | None = None) -> str:
    then = datetime.fromtimestamp(ts)
    today = datetime.fromtimestamp(now) if now is not None else datetime.now()
    clock = then.strftime("%H:%M")
    days = (today.date() - then.date()).days
    if days == 0:
        return clock
    if days == 1:
        return f"Yesterday {clock}"
    if 1 < days < 7:
        return then.strftime("%A ") + clock
    if then.year == today.year:
        return then.strftime("%-d %b ") + clock
    return then.strftime("%-d %b %Y ") + clock


def result_fields(action: dict) -> dict:
    result = action.get("result")
    return result if isinstance(result, dict) else {}


def is_shell(action: dict) -> bool:
    return action["name"] == SHELL_ACTION or "sandboxed" in result_fields(action)


def action_words(actions: list[dict]) -> str:
    """Names of what ran. The daemon records {"name", "arguments", "result"} for each. The result
    stays None if the turn ended first, and then nobody knows how it went."""
    words = []
    for action in actions:
        unfinished = " (no result)" if action.get("result") is None else ""
        words.append(action["name"] + (" (failed)" if "error" in result_fields(action)
                                       else unfinished))
    return ", ".join(words)


def run_words(action: dict) -> str:
    """How an action went, and where. Silence about the sandbox is not a claim either way."""
    fields = result_fields(action)
    if action.get("result") is None:
        return "No result, the request ended first"
    if fields.get("declined"):
        return "Did not run, not allowed"
    sandboxed = fields.get("sandboxed")
    where = "" if sandboxed is None else " in the sandbox" if sandboxed else " outside the sandbox"
    code = fields.get("exit_code")      # a command that fails is not an error to the handler
    outcome = f", exit code {code}" if code else ""
    return ("Failed" if "error" in fields else "Ran") + where + outcome


def answer_words(ask: dict) -> str:
    """No, no answer in time and a cancelled card are all recorded the same way."""
    words = "Allowed" if ask["allowed"] else "Not allowed"
    return f"{words} to run as root" if ask["level"] == "admin" else words


def as_text(value) -> str:
    """One key per line. Strings stay whole and unquoted, and several lines of them start on their
    own line, so command output keeps its shape."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        lines = []
        for key, item in value.items():
            text = as_text(item)
            lines.append(f"{key}:\n{text}" if "\n" in text else f"{key}: {text}")
        return "\n".join(lines)
    return json.dumps(value, ensure_ascii=False)


def arguments_of(action: dict):
    """What the model sent: a dict if it could write one, or else the text as it came."""
    arguments = action.get("arguments") or ""
    try:
        return json.loads(arguments)
    except (TypeError, ValueError):
        return arguments


def exact_command(action: dict) -> str:
    """What the handler says it ran, or else what it was asked to run."""
    arguments = arguments_of(action)
    wanted = arguments.get("command", "") if isinstance(arguments, dict) else arguments
    return as_text(result_fields(action).get("command") or wanted)


def action_fields(action: dict) -> list[tuple[str, str]]:
    """(label, text) for an entry opened up. Nothing is cut: this is where you check what ran."""
    fields = [(action["name"], run_words(action))]
    arguments = arguments_of(action)
    if is_shell(action):
        fields.append(("Command", exact_command(action)))
    elif arguments:
        fields.append(("Arguments", as_text(arguments)))
    for ask in action.get("asked") or []:
        fields.append((f"{answer_words(ask)}: {ask['title']}", ask["detail"]))
    if action.get("result") is not None:
        fields.append(("Result", as_text(action["result"])))
    return fields


def action_lines(action: dict) -> list[str]:
    """What the closed entry says about one action, beyond its name."""
    lines = []
    if is_shell(action):
        command = printable(exact_command(action), MAX_TITLE_CHARS)
        lines.append(f"{run_words(action)}: {command}" if command else run_words(action))
    elif result_fields(action).get("declined"):
        lines.append(f"Did not run {action['name']}")
    return lines + [f"{answer_words(ask)}: {printable(ask['title'], MAX_TITLE_CHARS)}"
                    for ask in action.get("asked") or []]


def history_details(entry: dict) -> str:
    """The lines shown under what was heard."""
    actions = entry.get("actions") or []
    lines = [entry.get("reply") or "No reply"]
    named = [action for action in actions
             if not is_shell(action) and not result_fields(action).get("declined")]
    if named:
        lines.append("Ran " + action_words(named))
    for action in actions:
        lines.extend(action_lines(action))
    status = entry.get("status", "ok")
    if status != "ok":
        lines.append(STATUS_WORDS.get(status, status))
    return "\n".join(lines)


def phone_choices(devices: list[dict] | None, saved: str) -> dict[str, str]:
    """{value: label} for the row. A saved device stays a choice when nothing lists it: it may just
    be out of reach, and showing Automatic would be untrue and the next save would lose it. The
    daemon also takes ids and any case, so a saved value that means a listed device is that one.
    None: the daemon hasn't answered yet, so nothing is known to be missing."""
    choices = {"": AUTOMATIC}
    for device in devices or []:
        if not device.get("paired", True):  # nothing can be sent to it, and it may not be yours
            continue
        meant = saved and saved.casefold() in (device["name"].casefold(), device["id"].casefold())
        choices[saved if meant else device["name"]] = device["name"]
    if saved not in choices:
        # The state first: the row cuts long labels short.
        choices[saved] = saved if devices is None else f"Not available: {saved}"
    return choices


def calendar_listing(reply) -> tuple[list[dict], str]:
    """ListCalendars sends a list, or {"problem": text} when the daemon can't reach calendars at
    all. The daemon words the problem, so voice and Settings say the same thing."""
    if isinstance(reply, dict):
        return [], shown(str(reply.get("problem") or NO_CALENDARS))
    return (reply if isinstance(reply, list) else []), ""


def calendar_choices(calendars: list[dict] | None, saved: str) -> dict[str, str]:
    """{id: label} for the row. Automatic is the system's own default calendar. As with phones, a
    saved calendar that isn't listed stays a choice, so the next save doesn't lose it."""
    choices = {"": AUTOMATIC}
    for calendar in calendars or []:
        account = calendar.get("account") or ""
        choices[calendar["id"]] = f"{calendar['name']} ({account})" if account else calendar["name"]
    if saved not in choices:
        choices[saved] = "…" if calendars is None else GONE_CALENDAR
    return choices


def cloud_note(enabled: bool, key_set: bool) -> str:
    """Says where requests really go, which takes both the switch and a key."""
    if enabled and key_set:
        return "Requests with several steps go to Anthropic"
    if enabled:
        return "No API key yet, so nothing goes to Anthropic"
    return "Nothing goes to Anthropic"


def extension_schema_dirs() -> list[Path]:
    data_home = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local/share")
    data_dirs = os.environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share"
    return [Path(root) / "gnome-shell/extensions" / EXTENSION_UUID / "schemas"
            for root in [data_home, *data_dirs.split(":")] if root]


def load_shell_settings() -> Gio.Settings | None:
    """The extension's settings, from the schemas it installed, or None if it isn't installed."""
    default = Gio.SettingsSchemaSource.get_default()
    for folder in extension_schema_dirs():
        if not (folder / "gschemas.compiled").exists():
            continue
        try:
            source = Gio.SettingsSchemaSource.new_from_directory(str(folder), default, False)
        except GLib.Error:
            continue
        schema = source.lookup(SHELL_SCHEMA, False)
        if schema and schema.has_key(SHORTCUT_KEY):
            return Gio.Settings.new_full(schema, None, None)
    return None


def shortcut_from_key(keyval: int, state: Gdk.ModifierType) -> str | None:
    """The accelerator for a key press, or None if it can't be a global shortcut."""
    mods = state & Gtk.accelerator_get_default_mod_mask()
    keyval = Gdk.keyval_to_lower(keyval)
    is_function_key = Gdk.KEY_F1 <= keyval <= Gdk.KEY_F35
    if not is_function_key and not mods & ~Gdk.ModifierType.SHIFT_MASK:
        return None  # it would fire on ordinary typing
    if not Gtk.accelerator_valid(keyval, mods or Gdk.ModifierType.CONTROL_MASK):
        return None
    return Gtk.accelerator_name(keyval, mods)


class Daemon:
    """Asynchronous access to org.tuxistant.Daemon. Replies arrive on the main loop."""

    def __init__(self, on_signal, on_owner, on_error):
        self.proxy = None
        self.on_signal = on_signal
        self.on_owner = on_owner
        self.on_error = on_error
        self.newest = {}

    def connect(self, done):
        """done(error). The call that follows starts the daemon if it can be activated."""
        if self.proxy:
            done(None)
            return

        def finish(_source, result):
            try:
                self.proxy = Gio.DBusProxy.new_for_bus_finish(result)
            except GLib.Error as e:
                done(e)
                return
            self.proxy.connect("g-signal", self._signal)
            self.proxy.connect("notify::g-name-owner", lambda *_: self.on_owner(self.owner()))
            done(None)

        Gio.DBusProxy.new_for_bus(
            Gio.BusType.SESSION, Gio.DBusProxyFlags.DO_NOT_LOAD_PROPERTIES, None,
            BUS_NAME, BUS_PATH, BUS_NAME, None, finish)

    def owner(self):
        return self.proxy.get_name_owner() if self.proxy else None

    def _signal(self, _proxy, _sender, name, params):
        self.on_signal(name, *params.unpack())

    def call(self, method, args=(), signature="", done=None, fail=None, timeout=READ_TIMEOUT_MS):
        params = GLib.Variant(f"({signature})", args) if signature else None

        def finish(proxy, result):
            try:
                reply = proxy.call_finish(result).unpack()
            except GLib.Error as e:
                (fail or self.on_error)(e)
                return
            if done:
                done(*reply)

        self.proxy.call(method, params, Gio.DBusCallFlags.NONE, timeout, None, finish)

    def fetch(self, method, done, args=(), signature="", fail=None, timeout=READ_TIMEOUT_MS):
        """Read a JSON value. Only the newest request per method counts, so a slow, stale reply
        can't overwrite a fresher one."""
        ticket = self.newest[method] = self.newest.get(method, 0) + 1

        def finish(text):
            if ticket != self.newest[method]:
                return
            try:
                value = json.loads(text)
            except ValueError:
                (fail or self.on_error)(GLib.Error(f"Unreadable reply to {method}"))
                return
            done(value)

        self.call(method, args, signature, finish, fail, timeout)


def plain_row(title, subtitle, **props):
    """A row for text from outside (theme names, transcripts), which must not be read as markup.
    Markup goes off before the text goes in."""
    row = Adw.ActionRow(use_markup=False, **props)
    row.set_title(shown(title))
    row.set_subtitle(shown(subtitle))
    return row


def detail_row(label, text):
    row = plain_row(label, text, subtitle_selectable=True)
    row.add_css_class("property")
    if label in RAW_FIELDS:
        row.add_css_class("raw")
    return row


class ChoiceRow(Adw.ActionRow):
    """One of a list of alternatives: activating it chooses it, and a check marks the choice."""

    def __init__(self, value, title, subtitle, on_choose, button):
        super().__init__(use_markup=False, activatable=True)
        self.value = value
        self.set_title(title)
        self.set_subtitle(subtitle)
        self.add_suffix(button)
        # Opacity, not visibility, so the buttons line up whichever row is chosen.
        self.check = Gtk.Image(icon_name="object-select-symbolic", opacity=0)
        self.add_suffix(self.check)
        self.connect("activated", lambda _row: on_choose(value))

    def set_chosen(self, chosen):
        self.check.set_opacity(1 if chosen else 0)
        # An int: SELECTED is a tristate, and a bool here is an assertion and reads as unselected.
        self.update_state([Gtk.AccessibleState.SELECTED], [int(chosen)])


class HistoryRow(Adw.ExpanderRow):
    """A request that ran something. Its actions are built on opening, as a result can be long
    and every new request rebuilds the page."""

    def __init__(self, title, subtitle, entry):
        super().__init__(use_markup=False)
        self.entry = entry
        self.built = False
        self.set_title(shown(title))
        self.set_subtitle(shown(subtitle))
        self.connect("notify::expanded", self.on_expanded)

    def on_expanded(self, *_):
        if self.get_expanded() and not self.built:
            self.built = True
            for action in self.entry["actions"]:
                for label, text in action_fields(action):
                    self.add_row(detail_row(label, text))


class ShortcutDialog(Adw.Dialog):
    def __init__(self, on_pick):
        super().__init__(title="Toggle shortcut", content_width=380, content_height=300)
        self.on_pick = on_pick
        self.page = Adw.StatusPage(
            icon_name="input-keyboard-symbolic", title="Press the new shortcut",
            description="Esc cancels. Backspace turns the shortcut off.")
        self.page.add_css_class("compact")
        view = Adw.ToolbarView(content=self.page)
        view.add_top_bar(Adw.HeaderBar())
        self.set_child(view)
        keys = Gtk.EventControllerKey(propagation_phase=Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self.on_key)
        self.add_controller(keys)

    def on_key(self, _controller, keyval, _keycode, state):
        plain = not state & Gtk.accelerator_get_default_mod_mask()
        if plain and keyval == Gdk.KEY_Escape:
            self.close()
        elif plain and keyval == Gdk.KEY_BackSpace:
            self.on_pick("")
            self.close()
        elif accel := shortcut_from_key(keyval, state):
            self.on_pick(accel)
            self.close()
        elif Gtk.accelerator_valid(Gdk.keyval_to_lower(keyval), Gdk.ModifierType.CONTROL_MASK):
            self.page.set_description("That would fire while typing. Add Ctrl, Alt or Super.")
        return True


class GeneralPage(Adw.PreferencesPage):
    def __init__(self, win):
        super().__init__()
        self.win = win
        self.syncing = False  # a change made here came from the daemon: don't send it back
        self.saved_server = ""
        self.saved_location = ""
        self.saved_instructions = ""
        self.saved_translate = ""
        self.saved_search = ""
        self.saved_model = ""
        self.saved_tapo_email = ""
        self.saved_home = ""
        self.devices = None  # what ListPhones last said: None until it has answered
        self.calendars = None   # the same for ListCalendars
        self.calendar_problem = ""
        self.calendar_labels, self.calendar_values = [], []
        self.phone_values = []
        self.phone_labels = []
        self.switches = {}
        self.spins = {}  # key: (row, config units per unit shown)
        self.shell = load_shell_settings()
        self.add(self._connection_group())
        self.add(self._pipeline_group())
        self.add(self._listening_group())
        self.add(self._weather_group())
        self.add(self._news_group())
        self.add(self._calendar_group())
        self.add(self._mail_group())
        self.add(self._timers_group())
        self.add(self._phone_group())
        self.add(self._translation_and_search_group())
        self.add(self._lights_group())
        self.add(self._home_group())
        self.add(self._cloud_group())
        self.add(self._shortcuts_group())
        self.add(self._instructions_group())

    def _connection_group(self):
        group = Adw.PreferencesGroup(title="Connection")
        self.server = Adw.EntryRow(title="Server address", show_apply_button=True)
        self.server.connect("apply", self.on_server_apply)
        self.server.connect("changed", lambda row: row.remove_css_class("error"))
        group.add(self.server)

        self.api_key, self.key_status, self.key_remove = self._key_row("API key", "api_key")
        group.add(self.api_key)
        return group

    def _key_row(self, title, key):
        """A row that only ever takes a key in: what is stored is never read back."""
        row = Adw.PasswordEntryRow(title=title, show_apply_button=True)
        row.connect("apply", self.on_key_apply, key)
        status = Gtk.Label(css_classes=["dim-label", "caption"])
        remove = Gtk.Button(
            icon_name="user-trash-symbolic", tooltip_text=f"Remove {title}",
            valign=Gtk.Align.CENTER, css_classes=["flat"], visible=False)
        remove.connect("clicked", self.on_key_remove, key)
        row.add_suffix(status)
        row.add_suffix(remove)
        return row, status, remove

    def _pipeline_group(self):
        group = Adw.PreferencesGroup(title="Pipeline")
        self.pipelines = {}
        for name, note in PIPELINES:
            first = next(iter(self.pipelines.values()), None)
            check = Gtk.CheckButton(group=first, valign=Gtk.Align.CENTER)
            check.connect("toggled", self.on_pipeline, name)
            row = plain_row(name, note, activatable_widget=check)
            row.add_prefix(check)
            group.add(row)
            self.pipelines[name] = check
        return group

    def _listening_group(self):
        group = Adw.PreferencesGroup(title="Listening")
        group.add(self._switch("conversation_mode", "Conversation mode",
                               "Keep listening briefly after a reply"))
        group.add(self._spin("listen_timeout_s", "Listen timeout",
                             "Seconds to wait for speech before giving up", 1, 120, 1))
        return group

    def _weather_group(self):
        group = Adw.PreferencesGroup(
            title="Weather", description="Open-Meteo receives the town you ask about")
        self.location = Adw.EntryRow(title="Home town", show_apply_button=True)
        self.location.connect("apply", self.on_location_apply)
        group.add(self.location)
        group.add(self._switch("fahrenheit", "Use Fahrenheit",
                               "Temperatures in Fahrenheit and wind in miles per hour"))
        return group

    def _news_group(self):
        group = Adw.PreferencesGroup(
            title="News", description="The source you choose sees your IP address when you ask")
        self.news = Adw.ComboRow(title="Source", use_markup=False)
        self.news.set_model(Gtk.StringList.new([name for name, _base, _feeds in SOURCES.values()]))
        self.news.connect("notify::selected", self.on_news)
        group.add(self.news)
        return group

    def _calendar_group(self):
        group = Adw.PreferencesGroup(
            title="Calendar",
            description="Calendars come from the accounts in GNOME Settings, under Online Accounts")
        self.calendar = Adw.ComboRow(title="Add events to", use_markup=False)
        self.calendar.connect("notify::selected", self.on_calendar)
        group.add(self.calendar)
        return group

    def _mail_group(self):
        group = Adw.PreferencesGroup(title="Mail", description=MAIL_NOTE)
        group.add(self._switch("mail_search", "Search my mail",
                               "Junk and Trash are never read, and sign-in codes are hidden"))
        return group

    def _timers_group(self):
        group = Adw.PreferencesGroup(title="Timers and reminders")
        group.add(self._switch("timer_chime", "Chime for timers",
                               "A short sound before the words when a timer or reminder comes "
                               "due. On a locked screen it is the only sound: the words wait "
                               "until you unlock"))
        return group

    def _phone_group(self):
        group = Adw.PreferencesGroup(title="Phone")
        self.phone = Adw.ComboRow(title="Default device", use_markup=False)
        self.phone.connect("notify::selected", self.on_phone)
        group.add(self.phone)
        return group

    def _translation_and_search_group(self):
        group = Adw.PreferencesGroup(
            title="Translation and search",
            description="Leave a server empty to use the main server's host: "
                        "port 5000 for translation, 8888 for search")
        self.translate = Adw.EntryRow(title="LibreTranslate server", show_apply_button=True)
        self.translate.connect("apply", self.on_translate_apply)
        self.translate.connect("changed", lambda row: row.remove_css_class("error"))
        group.add(self.translate)
        self.search = Adw.EntryRow(title="SearXNG server", show_apply_button=True)
        self.search.connect("apply", self.on_search_apply)
        self.search.connect("changed", lambda row: row.remove_css_class("error"))
        group.add(self.search)
        group.add(self._switch("fact_lookup", "Look up facts on Wikipedia",
                               "Questions of general knowledge are sent to Wikipedia, so answers "
                               "aren't made up. Off, Tux answers from memory and can be wrong."))
        return group

    def _lights_group(self):
        group = Adw.PreferencesGroup(title="Lights", description=LIGHTS_NOTE)
        self.tapo_email = Adw.EntryRow(title="TP-Link email", show_apply_button=True)
        self.tapo_email.connect("apply", self.on_tapo_email_apply)
        group.add(self.tapo_email)
        self.tapo_password, self.tapo_password_status, self.tapo_password_remove = self._key_row(
            "TP-Link password", "tapo_password")
        group.add(self.tapo_password)
        return group

    def _home_group(self):
        group = Adw.PreferencesGroup(title="Home Assistant", description=HOME_NOTE)
        self.home = Adw.EntryRow(title="Address", show_apply_button=True)
        self.home.connect("apply", self.on_home_apply)
        self.home.connect("changed", lambda row: row.remove_css_class("error"))
        group.add(self.home)
        self.home_token, self.home_token_status, self.home_token_remove = self._key_row(
            "Long-lived access token", "ha_token")
        group.add(self.home_token)
        return group

    def _cloud_group(self):
        group = Adw.PreferencesGroup(
            title="Cloud", description="For complex requests that need several steps")
        self.cloud = Adw.SwitchRow(title="Use Claude for complex requests")
        self.cloud.connect("notify::active", self.on_cloud)
        group.add(self.cloud)
        self.cloud_key, self.cloud_key_status, self.cloud_key_remove = self._key_row(
            "Anthropic API key", "cloud_api_key")
        group.add(self.cloud_key)
        self.cloud_model = Adw.EntryRow(title="Model", show_apply_button=True)
        self.cloud_model.connect("apply", self.on_model_apply)
        group.add(self.cloud_model)
        return group

    def _shortcuts_group(self):
        group = Adw.PreferencesGroup(title="Shortcuts")
        self.shortcut = Adw.ActionRow(title="Toggle shortcut", activatable=True)
        self.shortcut_label = Adw.ShortcutLabel(valign=Gtk.Align.CENTER, disabled_text="Off")
        self.shortcut.add_suffix(self.shortcut_label)
        self.shortcut.connect("activated", self.on_shortcut)
        group.add(self.shortcut)
        if self.shell:
            self.shell.connect(f"changed::{SHORTCUT_KEY}", lambda *_: self.sync_shortcut())
            self.sync_shortcut()
        else:
            self.shortcut_label.set_disabled_text("Unavailable")
            self.shortcut.set_sensitive(False)
            self.shortcut.set_subtitle("Install the Tuxistant GNOME Shell extension to set this")

        hold = self._switch("fn_hold_enabled", "Hold Fn to talk",
                            "Needs the tuxistant-fn helper")
        group.add(hold)
        duration = self._spin("fn_hold_ms", "Hold duration",
                              "Seconds to hold Fn before Tux starts listening",
                              0.3, 5, 0.1, scale=1000, digits=1)
        hide = self._switch("hide_on_release", "Hide on release",
                            "Hide Tux when Fn is released, even while it is still replying")
        group.add(duration)
        group.add(hide)
        for row in (duration, hide):
            hold.bind_property("active", row, "sensitive", GObject.BindingFlags.SYNC_CREATE)
        group.add(self._switch("dictation_enabled", "Dictation",
                               "Tap Fn, then hold it and speak, to type where your cursor is. "
                               "Ctrl+Super+D starts and stops it"))
        return group

    def _instructions_group(self):
        self.save_instructions = Gtk.Button(
            label="Save", sensitive=False, valign=Gtk.Align.CENTER)
        self.save_instructions.connect("clicked", self.on_instructions_save)
        group = Adw.PreferencesGroup(
            title="Assistant instructions", description="How Tux should behave and reply",
            header_suffix=self.save_instructions)
        self.instructions = Gtk.TextView(
            wrap_mode=Gtk.WrapMode.WORD_CHAR, accepts_tab=False, top_margin=10, bottom_margin=10,
            left_margin=12, right_margin=12)
        self.instructions.update_property(
            [Gtk.AccessibleProperty.LABEL], ["Assistant instructions"])
        self.instructions.get_buffer().connect("changed", self.on_instructions_changed)
        scroller = Gtk.ScrolledWindow(
            child=self.instructions, min_content_height=120, max_content_height=240,
            propagate_natural_height=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        frame = Gtk.Frame(child=scroller, css_classes=["view"])
        group.add(frame)
        return group

    def _switch(self, key, title, subtitle):
        row = Adw.SwitchRow(title=title, subtitle=subtitle)
        row.connect("notify::active", self.on_switch, key)
        self.switches[key] = row
        return row

    def _spin(self, key, title, subtitle, low, high, step, scale=1, digits=0):
        row = Adw.SpinRow.new_with_range(low, high, step)
        row.set_title(title)
        row.set_subtitle(subtitle)
        row.set_digits(digits)
        row.connect("notify::value", self.on_spin, key)
        self.spins[key] = (row, scale)
        return row

    def load(self):
        # Not worth an outage page: GSConnect can be slow, or the daemon older than this app.
        self.win.daemon.fetch("ListPhones", self.show_devices, timeout=PHONES_TIMEOUT_MS,
                              fail=lambda _error: self.show_devices([]))
        self.win.daemon.fetch("ListCalendars", self.show_calendars, timeout=CALENDARS_TIMEOUT_MS,
                              fail=lambda _error: self.show_calendars(
                                  {"problem": CALENDARS_FAILED}))

    def sync(self, config):
        """Show what the daemon holds, leaving alone anything the user is halfway through."""
        self.syncing = True
        # A daemon that hasn't restarted since an update may not send every key yet.
        server = config.get("server", self.saved_server)
        if self.server.get_text() == self.saved_server:
            self.set_row_text(self.server, server)
        self.saved_server = server
        location = config.get("location", self.saved_location)
        if self.location.get_text() == self.saved_location:
            self.set_row_text(self.location, location)
        self.saved_location = location
        translate = config.get("translate_server", "")
        if self.translate.get_text() == self.saved_translate:
            self.set_row_text(self.translate, translate)
        self.saved_translate = translate
        email = config.get("tapo_email", "")
        if self.tapo_email.get_text() == self.saved_tapo_email:
            self.set_row_text(self.tapo_email, email)
        self.saved_tapo_email = email
        home = config.get("ha_server", "")
        if self.home.get_text() == self.saved_home:
            self.set_row_text(self.home, home)
        self.saved_home = home
        search = config.get("search_server", "")
        if self.search.get_text() == self.saved_search:
            self.set_row_text(self.search, search)
        self.saved_search = search
        model = config.get("cloud_model") or DEFAULTS["cloud_model"]
        if self.cloud_model.get_text() == self.saved_model:
            self.set_row_text(self.cloud_model, model)
        self.saved_model = model
        # .get, as above, and a source this Settings doesn't know shows as the default.
        source = config.get("news_source", DEFAULT_SOURCE)
        self.news.set_selected(list(SOURCES).index(source if source in SOURCES else DEFAULT_SOURCE))
        self.show_phone()
        self.show_calendar()
        self.show_cloud()
        self.show_keys()
        if "pipeline" in config:
            for name, check in self.pipelines.items():
                check.set_active(name == config["pipeline"])
        for key, row in self.switches.items():
            if key in config and key not in self.win.timers:
                row.set_active(bool(config[key]))
        for key, (row, scale) in self.spins.items():
            if key in config and key not in self.win.timers:
                row.set_value(config[key] / scale)
        instructions = config.get("instructions", self.saved_instructions)
        if self.instructions_text() == self.saved_instructions:
            self.instructions.get_buffer().set_text(instructions)
        self.saved_instructions = instructions
        self.on_instructions_changed()
        self.syncing = False

    def set_row_text(self, row, text):
        # Text set from code counts as an edit and would leave the apply button showing.
        row.set_show_apply_button(False)
        row.set_text(text)
        row.set_show_apply_button(True)

    def show_keys(self):
        for status, remove, name in (
                (self.key_status, self.key_remove, "api_key"),
                (self.cloud_key_status, self.cloud_key_remove, "cloud_api_key"),
                (self.tapo_password_status, self.tapo_password_remove, "tapo_password"),
                (self.home_token_status, self.home_token_remove, "ha_token")):
            is_set = self.win.config.get(KEY_SET[name], False)
            words = KEY_WORDS.get(name, ("Key is set", "No key set"))
            status.set_label(words[0] if is_set else words[1])
            remove.set_visible(is_set)

    def show_devices(self, devices):
        self.devices = devices
        self.syncing = True
        self.show_phone()
        self.syncing = False

    def show_phone(self):
        """Only with syncing set: a new model selects its first item, and that isn't a choice."""
        saved = self.win.config.get("phone_device", "")
        choices = phone_choices(self.devices, saved)
        labels = [shown(label) for label in choices.values()]
        if labels != self.phone_labels:
            self.phone.set_model(Gtk.StringList.new(labels))
        self.phone_labels, self.phone_values = labels, list(choices)
        self.phone.set_selected(self.phone_values.index(saved))
        self.phone.set_subtitle(NO_PHONES if self.devices == [] else PHONE_NOTE)

    def show_calendars(self, reply):
        self.calendars, self.calendar_problem = calendar_listing(reply)
        self.syncing = True
        self.show_calendar()
        self.syncing = False

    def show_calendar(self):
        """Only with syncing set, as for the phone row."""
        saved = self.win.config.get("calendar_add_to", "")
        choices = calendar_choices(self.calendars, saved)
        labels = [shown(label) for label in choices.values()]
        if labels != self.calendar_labels:
            self.calendar.set_model(Gtk.StringList.new(labels))
        self.calendar_labels, self.calendar_values = labels, list(choices)
        self.calendar.set_selected(self.calendar_values.index(saved))
        empty = NO_CALENDARS if self.calendars == [] else CALENDAR_NOTE
        self.calendar.set_subtitle(self.calendar_problem or empty)

    def show_cloud(self):
        """The switch follows the config: after Cancel, or a reload behind an open dialog."""
        enabled = self.win.config.get("cloud_enabled", False)
        syncing, self.syncing = self.syncing, True
        self.cloud.set_active(enabled)
        self.syncing = syncing
        self.cloud.set_subtitle(cloud_note(enabled, self.win.config.get("cloud_key_set", False)))

    def on_server_apply(self, row):
        server = clean_server(row.get_text())
        if server is None:
            row.add_css_class("error")
            self.win.toast("Enter a host name and port, like myserver:8080")
            return
        self.set_row_text(row, server)
        if server != self.saved_server and self.win.config.get("api_key_set"):
            self.win.toast("A new server needs its own API key: enter it below")
        self.saved_server = server
        self.win.save("server", server)

    def on_location_apply(self, row):
        location = " ".join(row.get_text().split())
        self.set_row_text(row, location)
        self.saved_location = location
        self.win.save("location", location)

    def on_translate_apply(self, row):
        text = row.get_text().strip()
        server = clean_server(text) if text else ""
        if server is None:
            row.add_css_class("error")
            self.win.toast("Enter a host name and port, like myserver:5000")
            return
        self.set_row_text(row, server)
        self.saved_translate = server
        self.win.save("translate_server", server)

    def on_tapo_email_apply(self, row):
        email = row.get_text().strip()
        self.set_row_text(row, email)
        self.saved_tapo_email = email
        self.win.save("tapo_email", email)

    def on_home_apply(self, row):
        text = row.get_text().strip()
        server = homeassistant.clean_server(text) if text else ""
        if server is None:
            row.add_css_class("error")
            self.win.toast("Enter an address like homeassistant.local:8123")
            return
        self.set_row_text(row, server)
        if server != self.saved_home and self.win.config.get("ha_token_set"):
            # The daemon drops the token with the address it was given for. Show that now.
            self.win.toast("A new address needs its own token: enter it below")
            self.win.config["ha_token_set"] = False
            self.show_keys()
        self.saved_home = server
        self.win.save("ha_server", server)

    def on_search_apply(self, row):
        text = row.get_text().strip()
        server = clean_server(text) if text else ""
        if server is None:
            row.add_css_class("error")
            self.win.toast("Enter a host name and port, like myserver:8888")
            return
        self.set_row_text(row, server)
        self.saved_search = server
        self.win.save("search_server", server)

    def on_model_apply(self, row):
        model = row.get_text().strip() or DEFAULTS["cloud_model"]
        self.set_row_text(row, model)
        self.saved_model = model
        self.win.save("cloud_model", model)

    def on_key_apply(self, row, name):
        text = row.get_text()
        key = text if name == "tapo_password" else text.strip()    # a password may end in a space
        row.set_text("")
        if key.strip():
            self.set_key(name, key)

    def on_key_remove(self, _button, name):
        self.set_key(name, "")

    def set_key(self, name, key):
        self.win.config[KEY_SET[name]] = bool(key)
        self.show_keys()
        self.show_cloud()
        self.win.save(name, key)

    def on_news(self, row, _param):
        if not self.syncing:
            value = list(SOURCES)[row.get_selected()]
            self.win.config["news_source"] = value
            self.win.save("news_source", value)

    def on_calendar(self, row, _param):
        if not self.syncing:
            value = self.calendar_values[row.get_selected()]
            self.win.config["calendar_add_to"] = value
            self.win.save("calendar_add_to", value)

    def on_phone(self, row, _param):
        if not self.syncing:
            value = self.phone_values[row.get_selected()]
            self.win.config["phone_device"] = value
            self.win.save("phone_device", value)

    def on_cloud(self, row, _param):
        if self.syncing:
            return
        if row.get_active():
            self.ask_cloud()
        else:
            self.set_cloud(False)

    def ask_cloud(self):
        """Nothing is saved until the answer. Escape and clicking away count as Cancel."""
        dialog = Adw.AlertDialog(heading=CLOUD_HEADING, body=CLOUD_BODY)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("enable", "Turn On")
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.connect("response", self.on_cloud_response)
        dialog.present(self.win)

    def on_cloud_response(self, _dialog, response):
        if response == "enable":
            self.set_cloud(True)
        else:
            self.show_cloud()

    def set_cloud(self, enabled):
        self.win.config["cloud_enabled"] = enabled
        self.show_cloud()
        self.win.save("cloud_enabled", enabled)

    def on_pipeline(self, check, name):
        if check.get_active() and not self.syncing:
            self.win.save("pipeline", name)

    def on_switch(self, row, _param, key):
        if not self.syncing:
            self.win.save(key, row.get_active())

    def on_spin(self, row, _param, key):
        if not self.syncing:
            self.win.save_later(key, round(row.get_value() * self.spins[key][1]))

    def instructions_text(self):
        buffer = self.instructions.get_buffer()
        return buffer.get_text(*buffer.get_bounds(), False)

    def on_instructions_changed(self, *_):
        self.save_instructions.set_sensitive(self.instructions_text() != self.saved_instructions)

    def on_instructions_save(self, _button):
        self.saved_instructions = self.instructions_text()
        self.on_instructions_changed()
        self.win.save("instructions", self.saved_instructions)

    def sync_shortcut(self):
        accels = [a for a in self.shell.get_strv(SHORTCUT_KEY) if a]
        self.shortcut_label.set_accelerator(accels[0] if accels else "")

    def on_shortcut(self, _row):
        ShortcutDialog(self.set_shortcut).present(self.win)

    def set_shortcut(self, accel):
        self.shell.set_strv(SHORTCUT_KEY, [accel] if accel else [])


class VoicePage(Adw.Bin):
    def __init__(self, win):
        super().__init__()
        self.win = win
        self.groups = []  # (group, [(row, voice)])
        self.search = Gtk.SearchEntry(
            placeholder_text="Search voices", margin_start=12, margin_end=12)
        self.search.connect("search-changed", lambda _entry: self.apply_filter())
        self.list = Adw.PreferencesPage()
        self.stack = Gtk.Stack()
        self.stack.add_named(self.list, "list")
        self.stack.add_named(Adw.StatusPage(
            icon_name="edit-find-symbolic", title="No matching voices",
            description="Try a name, an accent or a gender"), "empty")
        view = Adw.ToolbarView(content=self.stack)
        # Same clamp as Adw.PreferencesPage, so the entry lines up with the list below it.
        view.add_top_bar(Adw.Clamp(child=self.search, margin_top=12))
        self.set_child(view)

    def load(self):
        self.win.daemon.fetch("ListVoices", self.fill)

    def fill(self, voices):
        for group, _rows in self.groups:
            self.list.remove(group)
        self.groups = []
        for accent, members in group_voices(voices).items():
            group = Adw.PreferencesGroup(title=accent)
            rows = [(self.make_row(voice), voice) for voice in members]
            for row, _voice in rows:
                group.add(row)
            self.list.add(group)
            self.groups.append((group, rows))
        self.select(self.win.config.get("voice"))
        self.apply_filter()

    def make_row(self, voice):
        play = Gtk.Button(
            icon_name="media-playback-start-symbolic", valign=Gtk.Align.CENTER,
            tooltip_text=f"Play a sample of {voice['name']}", css_classes=["flat", "circular"])
        play.connect("clicked", self.on_play, voice["id"])
        return ChoiceRow(voice["id"], voice["name"], voice["gender"], self.choose, play)

    def select(self, voice_id):
        for _group, rows in self.groups:
            for row, _voice in rows:
                row.set_chosen(row.value == voice_id)

    def choose(self, voice_id):
        self.win.config["voice"] = voice_id
        self.select(voice_id)
        self.win.save("voice", voice_id)

    def apply_filter(self):
        query = self.search.get_text()
        found = False
        for group, rows in self.groups:
            shown = 0
            for row, voice in rows:
                row.set_visible(voice_matches(voice, query))
                shown += row.get_visible()
            group.set_visible(shown > 0)
            found = found or shown > 0
        self.stack.set_visible_child_name("list" if found or not self.groups else "empty")

    def on_play(self, button, voice_id):
        spinner = Adw.Spinner()
        button.set_child(spinner)
        button.set_sensitive(False)

        def reset(*_):
            button.set_icon_name("media-playback-start-symbolic")
            button.set_sensitive(True)

        def failed(error):
            reset()
            self.win.on_error(error)

        self.win.daemon.call("PreviewVoice", (voice_id,), "s", reset, failed,
                             timeout=PREVIEW_TIMEOUT_MS)


class AppearancePage(Adw.PreferencesPage):
    def __init__(self, win):
        super().__init__()
        self.win = win
        self.rows = []
        self.problem_rows = []
        self.listed = None
        open_folder = Gtk.Button(label="Open themes folder", valign=Gtk.Align.CENTER)
        open_folder.connect("clicked", self.on_open_folder)
        self.themes = Adw.PreferencesGroup(title="Themes", header_suffix=open_folder)
        self.problems = Adw.PreferencesGroup(
            title="Themes with problems",
            description="These themes can't be used until they are fixed", visible=False)
        self.add(self.themes)
        self.add(self.problems)

    def load(self):
        self.win.daemon.fetch("ListThemes", self.fill, timeout=THEMES_TIMEOUT_MS)

    def fill(self, listing):
        if listing == self.listed:
            return
        self.listed = listing
        for row in self.rows:
            self.themes.remove(row)
        for row in self.problem_rows:
            self.problems.remove(row)
        self.rows = [self.make_row(theme) for theme in listing["themes"]]
        self.problem_rows = [self.make_problem(problem) for problem in listing["problems"]]
        for row in self.rows:
            self.themes.add(row)
        for row in self.problem_rows:
            self.problems.add(row)
        self.problems.set_visible(bool(self.problem_rows))
        self.select(self.win.config.get("theme"))

    def make_row(self, theme):
        button = Gtk.Button(label="Try", valign=Gtk.Align.CENTER,
                            tooltip_text=f"Show {theme['name']} for a few seconds")
        button.connect("clicked", self.on_try, theme["id"])
        return ChoiceRow(theme["id"], theme["name"], f"By {theme['author']}", self.choose, button)

    def make_problem(self, problem):
        row = plain_row(Path(problem["path"]).name, problem["error"], tooltip_text=problem["path"])
        row.add_prefix(Gtk.Image(icon_name="dialog-warning-symbolic", css_classes=["warning"]))
        return row

    def select(self, theme_id):
        for row in self.rows:
            row.set_chosen(row.value == theme_id)

    def choose(self, theme_id):
        self.win.config["theme"] = theme_id
        self.select(theme_id)
        self.win.save("theme", theme_id)

    def on_try(self, _button, theme_id):
        self.win.daemon.call("PreviewTheme", (theme_id,), "s")

    def on_open_folder(self, _button):
        try:
            themes.USER.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            self.win.toast(f"Could not create the themes folder: {e.strerror}")
            return
        launcher = Gtk.FileLauncher.new(Gio.File.new_for_path(str(themes.USER)))
        launcher.launch(self.win, None, self.on_folder_opened)

    def on_folder_opened(self, launcher, result):
        try:
            launcher.launch_finish(result)
        except GLib.Error as e:
            if not e.matches(Gtk.DialogError.quark(), Gtk.DialogError.DISMISSED):
                self.win.toast(f"Could not open the themes folder: {e.message}")


class SkillsPage(Adw.PreferencesPage):
    def __init__(self, win):
        super().__init__()
        self.win = win
        self.rows = []
        self.group = Adw.PreferencesGroup(
            title="Skills",
            description="What Tux can do for you. Risk follows from what a skill is allowed to do")
        self.add(self.group)

    def load(self):
        self.win.daemon.fetch("ListSkills", self.fill)

    def fill(self, skills):
        for row in self.rows:
            self.group.remove(row)
        self.rows = [self.make_row(skill) for skill in skills]
        for row in self.rows:
            self.group.add(row)

    def make_row(self, skill):
        row = plain_row(skill["name"],
                        f"{skill['description']}\n{permission_words(skill['permissions'])}")
        risk = skill["risk"]
        row.add_suffix(Gtk.Label(
            label=risk_label(risk), valign=Gtk.Align.CENTER,
            css_classes=["risk-badge", RISK_CLASSES.get(risk, "error")]))
        return row


class HistoryPage(Adw.Bin):
    def __init__(self, win):
        super().__init__()
        self.win = win
        self.rows = []
        self.clear_button = Gtk.Button(label="Clear", valign=Gtk.Align.CENTER)
        self.clear_button.connect("clicked", self.on_clear)
        self.group = Adw.PreferencesGroup(title="Recent requests", header_suffix=self.clear_button)
        page = Adw.PreferencesPage()
        page.add(self.group)
        self.stack = Gtk.Stack()
        self.stack.add_named(page, "list")
        self.stack.add_named(Adw.StatusPage(
            icon_name="document-open-recent-symbolic", title="No requests yet",
            description="What you ask Tux will show up here"), "empty")
        self.set_child(self.stack)

    def load(self):
        self.win.daemon.fetch("GetHistory", self.fill, (HISTORY_ROWS,), "i")

    def fill(self, entries):
        # A new request must not close what you are reading.
        opened = {row.entry["id"] for row in self.rows
                  if isinstance(row, HistoryRow) and row.get_expanded()}
        for row in self.rows:
            self.group.remove(row)
        self.rows = [self.make_row(entry) for entry in entries]
        for row in self.rows:
            self.group.add(row)
            if isinstance(row, HistoryRow):
                row.set_expanded(row.entry["id"] in opened)
        self.stack.set_visible_child_name("list" if entries else "empty")

    def make_row(self, entry):
        title, details = entry.get("heard") or "Nothing heard", history_details(entry)
        ran = entry.get("actions")
        row = HistoryRow(title, details, entry) if ran else plain_row(title, details)
        row.add_suffix(Gtk.Label(label=format_time(entry["ts"]), valign=Gtk.Align.START,
                                 margin_top=12, css_classes=["dim-label", "caption"]))
        return row

    def on_clear(self, _button):
        dialog = Adw.AlertDialog(
            heading="Clear history?",
            body="All recent requests will be deleted. This can't be undone.")
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("clear", "Clear")
        dialog.set_response_appearance("clear", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.connect("response", self.on_clear_response)
        dialog.present(self.win)

    def on_clear_response(self, _dialog, response):
        if response == "clear":
            self.win.daemon.call("ClearHistory")


class SettingsWindow(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Tuxistant settings",
                         default_width=860, default_height=760)
        self.set_size_request(360, 420)
        self.app = app
        self.state = "loading"
        self.config = {}
        self.timers = {}  # key: (source id, value) of saves waiting for the user to stop
        self.daemon = Daemon(self.on_signal, self.on_owner, self.on_error)
        self.general = GeneralPage(self)
        self.voices = VoicePage(self)
        self.appearance = AppearancePage(self)
        self.skills = SkillsPage(self)
        self.history = HistoryPage(self)

        self.root = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.root.add_named(self._plain_view(Adw.Spinner(
            halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER,
            width_request=48, height_request=48)), "loading")
        self.root.add_named(self._ready_view(), "ready")
        self.root.add_named(self._plain_view(self._offline_page()), "offline")
        self.root.set_visible_child_name("loading")
        self.toasts = Adw.ToastOverlay(child=self.root)
        self.set_content(self.toasts)
        self.connect("close-request", self.on_close)
        self.connect("notify::is-active", self.on_active)
        self.reload()

    def _plain_view(self, content):
        view = Adw.ToolbarView(content=content)
        view.add_top_bar(Adw.HeaderBar())
        return view

    def _ready_view(self):
        self.views = Adw.ViewStack()
        for name, title, icon, page in (
                ("general", "General", "preferences-system-symbolic", self.general),
                ("voice", "Voice", "audio-speakers-symbolic", self.voices),
                ("appearance", "Appearance", "applications-graphics-symbolic", self.appearance),
                ("skills", "Skills", "applications-system-symbolic", self.skills),
                ("history", "History", "document-open-recent-symbolic", self.history)):
            self.views.add_titled_with_icon(page, name, title, icon)
        self.views.connect("notify::visible-child", self.on_page_changed)
        self.sidebar = Adw.ViewSwitcherSidebar(stack=self.views)
        self.sidebar.connect("activated", lambda *_: self.split.set_show_content(True))
        self.content = Adw.NavigationPage(title="General", child=self._plain_view(self.views))
        self.split = Adw.NavigationSplitView(
            sidebar=Adw.NavigationPage(title=self.get_title(),
                                       child=self._plain_view(self.sidebar)),
            content=self.content)
        narrow = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 620sp"))
        narrow.add_setter(self.split, "collapsed", True)
        narrow.add_setter(self.sidebar, "mode", Adw.SidebarMode.PAGE)
        self.add_breakpoint(narrow)
        return self.split

    def on_page_changed(self, views, _param):
        self.content.set_title(views.get_page(views.get_visible_child()).get_title())

    def _offline_page(self):
        self.retry_button = Gtk.Button(label="Retry", halign=Gtk.Align.CENTER,
                                       css_classes=["pill", "suggested-action"])
        self.retry_button.connect("clicked", lambda _button: self.reload())
        return Adw.StatusPage(
            icon_name="network-offline-symbolic", title="Can't reach Tuxistant",
            description="The background service isn't running or isn't responding",
            child=self.retry_button)

    def show_state(self, state):
        self.state = state
        self.root.set_visible_child_name(state)

    def toast(self, message):
        toast = Adw.Toast(use_markup=False)
        toast.set_title(message)
        self.toasts.add_toast(toast)

    def reload(self):
        self.show_state("loading")
        self.daemon.connect(self.on_connected)

    def on_connected(self, error):
        if error:
            self.show_state("offline")
        else:
            self.daemon.fetch("GetConfig", self.on_first_config, timeout=START_TIMEOUT_MS,
                              fail=lambda _error: self.show_state("offline"))

    def on_first_config(self, config):
        self.apply_config(config)
        self.show_state("ready")
        for page in (self.general, self.voices, self.appearance, self.skills, self.history):
            page.load()

    def apply_config(self, config):
        self.config = config
        self.general.sync(config)
        self.voices.select(config.get("voice"))
        self.appearance.select(config.get("theme"))

    def load_config(self):
        self.daemon.fetch("GetConfig", self.apply_config)

    def on_signal(self, name, *_args):
        if self.state != "ready":
            return
        if name == "ConfigChanged":
            self.load_config()
        elif name == "HistoryChanged":
            self.history.load()

    def on_owner(self, owner):
        if owner is None:
            self.show_state("offline")
        elif self.state != "loading":
            self.reload()

    def on_error(self, error):
        if not_answering(error):
            self.show_state("offline")
        else:
            self.toast(error_text(error.message))

    def on_active(self, *_):
        # Themes dropped into the folder show up on coming back from the file manager, and a
        # phone on coming back from pairing it.
        if self.is_active() and self.state == "ready":
            self.general.load()
            self.appearance.load()

    def save(self, key, value):
        """Write a setting. If the daemon refuses, say so and show what it really holds."""
        self.app.hold()  # a save in flight must outlive a window closed right after it

        def done():
            self.app.release()

        def failed(error):
            self.app.release()
            self.on_error(error)
            if self.state == "ready":
                self.load_config()

        self.daemon.call("SetConfig", (key, json.dumps(value)), "ss", done, failed)

    def save_later(self, key, value):
        """Save once the user stops changing the value: a held spin button fires many times."""
        if key in self.timers:
            GLib.source_remove(self.timers[key][0])

        def fire():
            del self.timers[key]
            self.save(key, value)
            return GLib.SOURCE_REMOVE

        self.timers[key] = (GLib.timeout_add(SAVE_DELAY_MS, fire), value)

    def on_close(self, _window):
        for key, (source, value) in list(self.timers.items()):
            GLib.source_remove(source)
            del self.timers[key]
            self.save(key, value)
        return False


class SettingsApp(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        self.set_accels_for_action("window.close", ["<Primary>w", "<Primary>q"])

    def do_startup(self):
        Adw.Application.do_startup(self)
        provider = Gtk.CssProvider()
        provider.load_from_string(CSS)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    def do_activate(self):
        (self.get_active_window() or SettingsWindow(self)).present()

    def do_shutdown(self):
        Adw.Application.do_shutdown(self)
        # Skips GApplication's last drain of the main loop. A window closed during its first
        # frames leaves a GTK source that is always ready, so the drain would spin forever.
        self.quit()


def main(_argv=None) -> int:
    # Only the program name goes on: GApplication reads extra words as files to open.
    return SettingsApp().run(sys.argv[:1])


if __name__ == "__main__":
    sys.exit(main())
