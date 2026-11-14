"""Tuxistant Settings: a libadwaita window onto the daemon.

Everything goes through D-Bus and nothing blocks the UI. The one exception is the toggle
shortcut, which belongs to the Shell extension and lives in its GSettings schema.
"""
import json
import os
import re
import sys
from datetime import date, datetime
from pathlib import Path

import gi

gi.require_version("Adw", "1")
gi.require_version("Gtk", "4.0")
from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk, Pango  # noqa: E402

from . import BUS_NAME, BUS_PATH, highlight, homeassistant, themes  # noqa: E402
from . import config as cloud  # noqa: E402
from .config import CLOUD_PRIVACY_URL, CLOUD_PROVIDER, DATA_DIR, DEFAULTS, clean_server  # noqa: E402
from .gate import MAX_TITLE_CHARS, printable  # noqa: E402
from .gate import shown as name_shown  # noqa: E402
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
SKILLS_FOLDER = DATA_DIR / "skills"   # where your skills and recipes go, as skillhost.USER_DIR
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
CLOUD_HEADING = f"Send multi-step requests to {CLOUD_PROVIDER}?"
# What the user agrees to, as config.CLOUD_CONSENT_VERSION. Bump that when what is sent changes.
CLOUD_BODY = ("While this is on, a request that needs several steps goes to {provider}'s model "
              "{model}, with your API key, instead of to your own server.")
CLOUD_DETAILS = (
    "What leaves this computer, for those requests:\n"
    "• your request, as Tux puts it into words\n"
    "• the names and descriptions of the actions Tux can take, including skills you added\n"
    "• what each step gives back, such as file and folder names, command output, calendar "
    "events and search results\n\n"
    "What never leaves without your click on that one item:\n"
    "• what is inside a file\n"
    "• what Home Assistant answers, and your list of timers and reminders\n"
    "• the request itself, once the conversation has read something private\n\n"
    f"Your voice, dictation and single-step requests never go to {CLOUD_PROVIDER}. What it "
    "receives is handled under its privacy policy."
)
CLOUD_AGREE = "I understand and agree"
CLOUD_POLICY = f"{CLOUD_PROVIDER}'s privacy policy"
RESTART_TO_AGREE = "Tuxistant must be restarted before it can ask you to agree"
MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August", "September",
          "October", "November", "December")     # not the locale's: the rest is in English
SENT_AS = "As the model gets it"
RAW_FIELDS = ("Command", "Arguments", "Result", SENT_AS)    # shown as they are, so columns stay
# Each skill's settings, shown on its page, by the SettingsBox rows that make them. A setting two
# skills use, such as the phone, is on both pages: only one page is open at a time, and each is
# built when it opens. Only skills that come with Tuxistant: the ids are reserved for them.
SKILL_SETTINGS = {
    "org.tuxistant.weather": ("weather",), "org.tuxistant.news": ("news",),
    "org.tuxistant.calendar_add": ("calendar",), "org.tuxistant.mail": ("mail",),
    "org.tuxistant.timers": ("timer",), "org.tuxistant.phone": ("phone",),
    "org.tuxistant.phone_send": ("phone",), "org.tuxistant.system": ("tailscale",),
    "org.tuxistant.translate": ("translation",),
    "org.tuxistant.translate_selection": ("translation",),
    "org.tuxistant.knowledge": ("fact", "search"), "org.tuxistant.websearch": ("search",),
    "org.tuxistant.research": ("search",), "org.tuxistant.lights": ("lights",),
    "org.tuxistant.home": ("home",), "org.tuxistant.home_status": ("home",),
    "org.tuxistant.task": ("cloud",),
}
# What each set of rows is called when no skill that shows it is loaded, as when Lights can't run
# without its sandbox: the settings still need a place, if only to remove a stored password.
SETTINGS_TITLES = {
    "weather": "Weather", "news": "News", "calendar": "Add to calendar", "mail": "Mail",
    "timer": "Timers and reminders", "phone": "Phone", "tailscale": "This computer",
    "translation": "Translation", "search": "Web search", "fact": "General knowledge",
    "lights": "Lights", "home": "Home Assistant", "cloud": "Multi-step tasks",
}
NOT_LOADED = ("The skill these settings are for isn't loaded now, so Tux can't use it. They are "
              "kept, and Skills with problems says why")
# A manifest's colours, light and dark, each at least 5:1 on libadwaita's view background.
SYNTAX = {
    highlight.KEY: ("#1a5fb4", "#99c1f1"),
    highlight.STRING: ("#1d7a40", "#8ff0a4"),
    highlight.NUMBER: ("#b5400a", "#ffbe6f"),
    highlight.COMMENT: ("#6a696e", "#a3a2a7"),
    highlight.MARK: ("#6a696e", "#a3a2a7"),
}
# Wrapping at words, Pango pays more for each line of a paragraph with nowhere to break: 64 KB
# of "a,a,a" took 1.7 s, against 0.1 s wrapped at any character.
LONG_LINE = 1000
MAX_FILES_SHOWN = 100       # a code skill may have 2,000, and a row each takes seconds to build
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


def path_shown(path) -> str:
    """A file or folder name from outside. A line break in one could fake a line of what is
    shown, such as a second file or another origin, so it shows as an escape."""
    return name_shown(path, sys.maxsize)


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


def calendar_listing(reply) -> tuple[list[dict] | None, str]:
    """ListCalendars sends a list, or {"problem": text} when the daemon can't reach calendars at
    all. The daemon words the problem, so voice and Settings say the same thing. A problem gives
    no list, as it says nothing about whether the saved calendar is still there."""
    if isinstance(reply, dict):
        return None, shown(str(reply.get("problem") or NO_CALENDARS))
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


def cloud_body(model: str) -> str:
    return CLOUD_BODY.format(provider=CLOUD_PROVIDER, model=shown(model))


def older_daemon(config: dict) -> bool:
    """A daemon that hasn't restarted since an update sends no cloud_consent. It can't record an
    agreement, and uses the cloud with the switch and a key alone, as it always did."""
    return "cloud_consent" not in config


def cloud_on(config: dict) -> bool:
    """What the switch shows: on only with an agreement that covers what is sent now. With an
    older daemon, what it holds, so that Cloud can still be switched off."""
    if older_daemon(config):
        return config.get("cloud_enabled") is True
    return config.get("cloud_enabled") is True and cloud.cloud_agreed(config)


def cloud_note(config: dict) -> str:
    """Says where requests really go, which takes the switch, the agreement and a key. The same
    test as the daemon's, config.cloud_in_use, so the two can't disagree, or an older daemon's."""
    nothing = f"nothing goes to {CLOUD_PROVIDER}"
    if older_daemon(config):
        if config.get("cloud_enabled") is not True:
            return f"Nothing goes to {CLOUD_PROVIDER}. {RESTART_TO_AGREE}"
        if not config.get("cloud_key_set"):
            return f"No API key yet, so {nothing}. {RESTART_TO_AGREE}"
        return (f"{RESTART_TO_AGREE}. Until then, requests with several steps go to "
                f"{CLOUD_PROVIDER}")
    if config.get("cloud_enabled") is not True:
        return f"Nothing goes to {CLOUD_PROVIDER}"
    if not cloud.cloud_agreed(config):
        consent = config.get("cloud_consent")
        if not consent or not isinstance(consent, dict):
            return f"On, but not agreed to, so {nothing}. Turn it on to read what is sent and agree"
        if cloud.cloud_agreed({**config, "cloud_model": consent.get("model")}):
            return (f"You agreed for another model, so {nothing}. Turn it on to agree for "
                    f"{shown(cloud.cloud_model(config))}")
        return (f"What is sent has changed since you agreed, so {nothing}. Turn it on to read "
                "it and agree again")
    if not config.get("cloud_key_set"):
        return f"No API key yet, so {nothing}"
    return f"Requests with several steps go to {CLOUD_PROVIDER}"


def day_words(text) -> str:
    try:
        day = date.fromisoformat(text)
    except (TypeError, ValueError):
        return "a date that can't be read"
    return f"{day.day} {MONTHS[day.month - 1]} {day.year}"


def consent_words(config: dict) -> tuple[str, str] | None:
    """The recorded agreement as (title, subtitle), or None when there is none."""
    consent = config.get("cloud_consent")
    if not consent or not isinstance(consent, dict):
        return None
    what = (f"For {shown(str(consent.get('model')))} at {shown(str(consent.get('provider')))}, "
            f"version {shown(str(consent.get('version')))} of what is sent")
    if not cloud.cloud_agreed(config):
        what += ". It doesn't cover what would be sent now, so nothing goes"
    return f"You agreed on {day_words(consent.get('date'))}", what


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


class SettingsBox:
    """Rows that edit settings. General holds the app's own, and each skill's page holds that
    skill's, built when the page opens: a setting shows in one place at a time, so two rows can't
    disagree about it. Only what was built is shown, synced and saved."""

    ROWS = ("location", "news", "calendar", "phone", "translate", "search", "tapo_email",
            "tapo_password", "home", "home_token", "cloud", "cloud_consent", "cloud_key",
            "cloud_model")

    def __init__(self, win):
        self.win = win
        self.syncing = False  # a change made here came from the daemon: don't send it back
        self.saved_location = ""
        self.saved_translate = ""
        self.saved_search = ""
        self.saved_model = ""
        self.saved_tapo_email = ""
        self.saved_home = ""
        self.consent_dialog = None  # the agreement, while it is open
        self.devices = None  # what ListPhones last said: None until it has answered
        self.calendars = None   # the same for ListCalendars
        self.calendar_problem = ""
        self.calendar_labels, self.calendar_values = [], []
        self.phone_values = []
        self.phone_labels = []
        self.switches = {}
        self.spins = {}  # key: (row, config units per unit shown)
        self.keys = {}   # name: (row, status, remove)
        for name in self.ROWS:
            setattr(self, name, None)

    # The rows of each skill's settings. Each adds its rows to a group and returns its note.

    def weather_rows(self, group):
        self.location = Adw.EntryRow(title="Home town", show_apply_button=True)
        self.location.connect("apply", self.on_location_apply)
        group.add(self.location)
        group.add(self._switch("fahrenheit", "Use Fahrenheit",
                               "Temperatures in Fahrenheit and wind in miles per hour"))
        return "Open-Meteo receives the town you ask about"

    def news_rows(self, group):
        self.news = Adw.ComboRow(title="Source", use_markup=False)
        self.news.set_model(Gtk.StringList.new([name for name, _base, _feeds in SOURCES.values()]))
        self.news.connect("notify::selected", self.on_news)
        group.add(self.news)
        return "The source you choose sees your IP address when you ask"

    def calendar_rows(self, group):
        self.calendar = Adw.ComboRow(title="Add events to", use_markup=False)
        self.calendar.connect("notify::selected", self.on_calendar)
        group.add(self.calendar)
        return "Calendars come from the accounts in GNOME Settings, under Online Accounts"

    def mail_rows(self, group):
        group.add(self._switch("mail_search", "Search my mail",
                               "Junk and Trash are skipped, and sign-in codes are hidden where "
                               "they are recognised"))
        return MAIL_NOTE

    def timer_rows(self, group):
        group.add(self._switch("timer_chime", "Chime for timers",
                               "A short sound before the words when a timer or reminder comes "
                               "due. On a locked screen it is the only sound: the words wait "
                               "until you unlock"))
        return ""

    def phone_rows(self, group):
        self.phone = Adw.ComboRow(title="Default device", use_markup=False)
        self.phone.connect("notify::selected", self.on_phone)
        group.add(self.phone)
        return ""

    def tailscale_rows(self, group):
        group.add(self._switch("say_tailscale_address", "Say my Tailscale address",
                               "When you ask for your IP address, also say this computer's "
                               "Tailscale address"))
        return ""

    def translation_rows(self, group):
        self.translate = Adw.EntryRow(title="LibreTranslate server", show_apply_button=True)
        self.translate.connect("apply", self.on_translate_apply)
        self.translate.connect("changed", lambda row: row.remove_css_class("error"))
        group.add(self.translate)
        return "Leave the server empty to use the main server's host, port 5000"

    def search_rows(self, group):
        self.search = Adw.EntryRow(title="SearXNG server", show_apply_button=True)
        self.search.connect("apply", self.on_search_apply)
        self.search.connect("changed", lambda row: row.remove_css_class("error"))
        group.add(self.search)
        return "Leave the server empty to use the main server's host, port 8888"

    def fact_rows(self, group):
        group.add(self._switch("fact_lookup", "Look up facts on Wikipedia",
                               "Questions of general knowledge are sent to Wikipedia, so answers "
                               "aren't made up. Off, Tux answers from memory and can be wrong."))
        return ""

    def lights_rows(self, group):
        self.tapo_email = Adw.EntryRow(title="TP-Link email", show_apply_button=True)
        self.tapo_email.connect("apply", self.on_tapo_email_apply)
        group.add(self.tapo_email)
        self.tapo_password, self.tapo_password_status, self.tapo_password_remove = self._key_row(
            "TP-Link password", "tapo_password")
        group.add(self.tapo_password)
        return LIGHTS_NOTE

    def home_rows(self, group):
        self.home = Adw.EntryRow(title="Address", show_apply_button=True)
        self.home.connect("apply", self.on_home_apply)
        self.home.connect("changed", lambda row: row.remove_css_class("error"))
        group.add(self.home)
        self.home_token, self.home_token_status, self.home_token_remove = self._key_row(
            "Long-lived access token", "ha_token")
        group.add(self.home_token)
        return HOME_NOTE

    def cloud_rows(self, group):
        # Plain text: the subtitles name the model, which anyone on the bus can set.
        self.cloud = Adw.SwitchRow(title="Use Claude for complex requests", use_markup=False)
        self.cloud.connect("notify::active", self.on_cloud)
        group.add(self.cloud)
        self.cloud_consent = Adw.ActionRow(use_markup=False, visible=False,
                                           css_classes=["property"])
        self.withdraw = Gtk.Button(label="Withdraw", valign=Gtk.Align.CENTER,
                                   css_classes=["destructive-action"],
                                   tooltip_text="Turn it off and forget that you agreed")
        self.withdraw.connect("clicked", self.on_withdraw)
        self.cloud_consent.add_suffix(self.withdraw)
        group.add(self.cloud_consent)
        self.cloud_key, self.cloud_key_status, self.cloud_key_remove = self._key_row(
            "Anthropic API key", "cloud_api_key")
        group.add(self.cloud_key)
        self.cloud_model = Adw.EntryRow(title="Model", show_apply_button=True)
        self.cloud_model.connect("apply", self.on_model_apply)
        group.add(self.cloud_model)
        return "For complex requests that need several steps"

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
        self.keys[key] = (row, status, remove)
        return row, status, remove

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
        if self.phone:
            self.win.daemon.fetch("ListPhones", self.show_devices, timeout=PHONES_TIMEOUT_MS,
                                  fail=lambda _error: self.show_devices([]))
        if self.calendar:
            self.win.daemon.fetch("ListCalendars", self.show_calendars,
                                  timeout=CALENDARS_TIMEOUT_MS,
                                  fail=lambda _error: self.show_calendars(
                                      {"problem": CALENDARS_FAILED}))

    def sync(self, config):
        """Show what the daemon holds, leaving alone anything the user is halfway through."""
        self.syncing = True
        # A daemon that hasn't restarted since an update may not send every key yet.
        if self.location:
            self.saved_location = self.sync_entry(
                self.location, self.saved_location, config.get("location", self.saved_location))
        if self.translate:
            self.saved_translate = self.sync_entry(
                self.translate, self.saved_translate, config.get("translate_server", ""))
        if self.tapo_email:
            self.saved_tapo_email = self.sync_entry(
                self.tapo_email, self.saved_tapo_email, config.get("tapo_email", ""))
        if self.home:
            self.saved_home = self.sync_entry(self.home, self.saved_home,
                                              config.get("ha_server", ""))
        if self.search:
            self.saved_search = self.sync_entry(self.search, self.saved_search,
                                                config.get("search_server", ""))
        if self.cloud_model:
            self.saved_model = self.sync_entry(
                self.cloud_model, self.saved_model,
                config.get("cloud_model") or DEFAULTS["cloud_model"])
        if self.news:
            # .get, as above, and a source this Settings doesn't know shows as the default.
            source = config.get("news_source", DEFAULT_SOURCE)
            self.news.set_selected(
                list(SOURCES).index(source if source in SOURCES else DEFAULT_SOURCE))
        self.show_phone()
        self.show_calendar()
        self.show_cloud()
        self.show_keys()
        for key, row in self.switches.items():
            if key in config and key not in self.win.timers:
                row.set_active(bool(config[key]))
        for key, (row, scale) in self.spins.items():
            if key in config and key not in self.win.timers:
                row.set_value(config[key] / scale)
        self.syncing = False

    def sync_entry(self, row, saved, value):
        """Shows the daemon's value unless the row holds an edit. Returns the new saved value."""
        if row.get_text() == saved:
            self.set_row_text(row, value)
        return value

    def set_row_text(self, row, text):
        # Text set from code counts as an edit and would leave the apply button showing.
        row.set_show_apply_button(False)
        row.set_text(text)
        row.set_show_apply_button(True)

    def show_keys(self):
        for name, (_row, status, remove) in self.keys.items():
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
        if not self.phone:
            return
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
        if not self.calendar:
            return
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
        """The switch follows the config: after Cancel, or a reload behind an open dialog. It
        shows on only when the daemon would really use the cloud, agreement and all."""
        if not self.cloud:
            return
        config = self.win.config
        syncing, self.syncing = self.syncing, True
        self.cloud.set_active(cloud_on(config))
        self.syncing = syncing
        self.cloud.set_subtitle(cloud_note(config))
        words = consent_words(config)
        self.cloud_consent.set_visible(words is not None)
        if words:
            self.cloud_consent.set_title(words[0])
            self.cloud_consent.set_subtitle(words[1])

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
        if not cloud.valid_model(model):    # the daemon refuses it too: no agreement could name it
            self.win.toast(cloud.NOT_A_MODEL)
            self.set_row_text(row, self.saved_model)
            return
        self.set_row_text(row, model)
        if (model != cloud.cloud_model(self.win.config) and cloud_on(self.win.config)
                and not older_daemon(self.win.config)):
            # The agreement named the model. Another one is a new agreement, made with it.
            self.ask_cloud(model, cancelled=lambda: self.set_row_text(row, self.saved_model))
            return
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
        if not row.get_active():
            self.set_cloud(False)
        elif older_daemon(self.win.config):     # it would refuse the agreement, and not ask
            self.win.toast(RESTART_TO_AGREE)
            # Not from inside the switch's own change: the row would push it back on, and save.
            GLib.idle_add(lambda: self.show_cloud() or GLib.SOURCE_REMOVE)
        elif cloud.cloud_agreed(self.win.config):     # agreed already, to all of this
            self.set_cloud(True)
        else:
            self.ask_cloud(cloud.cloud_model(self.win.config))

    def ask_cloud(self, model, cancelled=None):
        """Nothing is saved until the answer. Turn On waits for the tick, and Enter, Escape and
        clicking away all count as Cancel."""
        dialog = Adw.AlertDialog(heading=CLOUD_HEADING, body=cloud_body(model))
        dialog.details = Gtk.Label(label=CLOUD_DETAILS, use_markup=False, wrap=True, xalign=0,
                                   css_classes=["body"])
        dialog.agree = Gtk.CheckButton(label=CLOUD_AGREE)
        dialog.policy = Gtk.LinkButton(uri=CLOUD_PRIVACY_URL, label=CLOUD_POLICY,
                                       halign=Gtk.Align.START)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.append(dialog.details)
        box.append(dialog.policy)
        box.append(dialog.agree)
        dialog.set_extra_child(box)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("enable", "Turn On")
        dialog.set_response_appearance("enable", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_response_enabled("enable", False)
        dialog.agree.connect("toggled", lambda check: dialog.set_response_enabled(
            "enable", check.get_active()))
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.connect("response", self.on_cloud_response, model, cancelled)
        self.consent_dialog = dialog
        dialog.present(self.win)

    def on_cloud_response(self, dialog, response, model, cancelled):
        self.consent_dialog = None
        if response == "enable" and dialog.agree.get_active():
            self.agree(model)
        else:
            if cancelled:
                cancelled()
            self.show_cloud()

    def agree(self, model):
        """One save: the daemon records today's date, turns the cloud on and uses this model."""
        asked = {"version": cloud.CLOUD_CONSENT_VERSION, "provider": CLOUD_PROVIDER,
                 "model": model}
        self.win.config.update(cloud_enabled=True, cloud_model=model, cloud_consent={
            **asked, "date": date.today().isoformat()})
        self.saved_model = model
        if self.cloud_model:
            self.set_row_text(self.cloud_model, model)
        self.show_cloud()
        self.win.save("cloud_consent", asked)

    def on_withdraw(self, _button):
        self.win.config.update(cloud_enabled=False, cloud_consent={})
        self.show_cloud()
        self.win.save("cloud_consent", {})

    def set_cloud(self, enabled):
        self.win.config["cloud_enabled"] = enabled
        self.show_cloud()
        self.win.save("cloud_enabled", enabled)

    def on_switch(self, row, _param, key):
        if not self.syncing:
            self.win.save(key, row.get_active())

    def on_spin(self, row, _param, key):
        if not self.syncing:
            self.win.save_later(key, round(row.get_value() * self.spins[key][1]))


class GeneralPage(Adw.PreferencesPage, SettingsBox):
    """The app's own settings: the server and its key, listening, shortcuts and instructions.
    A skill's settings are on its page under Skills."""

    def __init__(self, win):
        Adw.PreferencesPage.__init__(self)
        SettingsBox.__init__(self, win)
        self.saved_server = ""
        self.saved_instructions = ""
        self.shell = load_shell_settings()
        self.add(self._connection_group())
        self.add(self._pipeline_group())
        self.add(self._listening_group())
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
                               "Tap Fn twice, speak, and tap it again to type where your "
                               "cursor is. Or tap it, then hold it while you speak. "
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

    def sync(self, config):
        SettingsBox.sync(self, config)
        self.syncing = True
        server = config.get("server", self.saved_server)
        self.saved_server = self.sync_entry(self.server, self.saved_server, server)
        if "pipeline" in config:
            for name, check in self.pipelines.items():
                check.set_active(name == config["pipeline"])
        instructions = config.get("instructions", self.saved_instructions)
        if self.instructions_text() == self.saved_instructions:
            self.instructions.get_buffer().set_text(instructions)
        self.saved_instructions = instructions
        self.on_instructions_changed()
        self.syncing = False

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

    def on_pipeline(self, check, name):
        if check.get_active() and not self.syncing:
            self.win.save("pipeline", name)

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
        open_folder(self.win, themes.USER, "themes", self.on_folder_opened)

    def on_folder_opened(self, launcher, result):
        folder_opened(self.win, "themes", launcher, result)


def open_folder(win, folder: Path, what: str, done):
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        win.toast(f"Could not create the {what} folder: {e.strerror}")
        return
    launcher = Gtk.FileLauncher.new(Gio.File.new_for_path(str(folder)))
    launcher.launch(win, None, done)


def folder_opened(win, what: str, launcher, result):
    try:
        launcher.launch_finish(result)
    except GLib.Error as e:
        if not e.matches(Gtk.DialogError.quark(), Gtk.DialogError.DISMISSED):
            win.toast(f"Could not open the {what} folder: {e.message}")


def skill_subtitle(skill: dict) -> str:
    """Whose it is comes first, as a skill you added was written by anyone at all."""
    whose = ""
    if skill.get("source") == "yours":
        whose = "A recipe you added. " if skill.get("recipe") else "A skill you added. "
    return f"{whose}{skill['description']}\n{permission_words(skill['permissions'])}"


def skill_problems(problems) -> list[tuple[str, str, str]]:
    """(title, problem, full path) for each skill that didn't load, from SkillProblems."""
    if not isinstance(problems, dict):
        return []
    return [(path_shown(Path(str(path)).name or str(path)), str(problem), path_shown(path))
            for path, problem in sorted(problems.items())]


def skill_origin(skill: dict) -> str:
    if skill.get("source") == "yours":
        return "A recipe you added" if skill.get("recipe") else "A code skill you added"
    if skill.get("source") == "bundled":
        return "Comes with Tuxistant: a code skill"
    return "Built into Tuxistant"


def module_file(module: str) -> str:
    return module.replace(".", "/") + ".py"


def code_words(details: dict) -> str:
    """What stands behind the manifest, as the YAML alone isn't the skill."""
    if details.get("recipe"):
        return ("It has no code of its own. Tux does what the file says with its own code, in "
                "tuxistant/recipes.py, and a recipe can only ask the hosts it names, open the "
                "app or page it names, or say a sentence")
    if details.get("source") in ("yours", "bundled"):
        return ("Its own program, in the files below. It runs in a sandbox that sees its own "
                "folder and none of your home folder, with the network only if it asks for it")
    modules = sorted({module_file(a["module"]) for a in details.get("actions", [])
                      if a.get("module")})
    where = f" in {', '.join(modules)}" if modules else ""
    return (f"Built in: Python code{where}. The manifest says what the model is offered, and "
            "the code decides what really happens")


def permission_list(permissions: dict) -> list[tuple[str, str]]:
    """(what, where) for each permission asked for, in words. Unknown ones still show."""
    found = []
    for key in [k for k in PERMISSION_WORDS if k in permissions] + [
            k for k in permissions if k not in PERMISSION_WORDS]:
        value = permissions[key]
        if not value:
            continue
        words = PERMISSION_WORDS.get(key) or "use " + str(key).replace("_", " ")
        if isinstance(value, str):
            value = [value]
        where = "Anywhere" if key == "network" and value is True else ""
        if isinstance(value, (list, tuple)):
            where = ", ".join("your own server" if key == "network" and name == HOME_SERVER
                              else str(name) for name in value)
        found.append((words[0].upper() + words[1:], where))
    return found


def risk_words(risk: str, because: list | None, permissions: dict) -> str:
    """Why the risk is what it is. An older daemon doesn't say, so then the permissions do."""
    if because is None:
        return permission_words(permissions)
    if not because:
        return "It asks for nothing that raises the risk"
    reasons = [PERMISSION_WORDS.get(key) or "use " + str(key).replace("_", " ")
               for key in because]
    head = ", ".join(reasons[:-1])
    return f"Because it can {head + ' and ' if head else ''}{reasons[-1]}"


def held_back_words(action: dict) -> str:
    if action.get("offered", True):
        return "The model is offered this"
    if action.get("held_back") == "switched_off":
        return "Not offered to the model until it is switched on in the settings above"
    return "Not offered to the model until its settings above are filled in"


def param_words(name: str, schema, required: list) -> tuple[str, str]:
    """A parameter as a row: its name, and what the model is told of it."""
    title = f"{name} (required)" if name in required else name
    if not isinstance(schema, dict):
        return title, as_text(schema)
    rest = as_text({key: value for key, value in schema.items() if key != "description"})
    lines = [as_text(schema["description"])] if "description" in schema else []
    return title, "\n".join(lines + [rest] if rest else lines)


def manifest_size(text: str) -> str:
    size = len(text.encode())
    return f"{size} bytes" if size < 1024 else f"{size / 1024:.1f} KB"


class SkillRow(Adw.ActionRow):
    """One skill. Activating it opens its page."""

    def __init__(self, skill):
        super().__init__(use_markup=False, activatable=True)
        self.skill = skill
        self.set_title(shown(skill["name"]))
        self.set_subtitle(shown(skill_subtitle(skill)))
        risk = skill["risk"]
        self.add_suffix(Gtk.Label(
            label=risk_label(risk), valign=Gtk.Align.CENTER,
            css_classes=["risk-badge", RISK_CLASSES.get(risk, "error")]))
        self.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))


class SkillSettings(SettingsBox):
    """One skill's settings, on its page."""

    def __init__(self, win, names):
        super().__init__(win)
        self.group = Adw.PreferencesGroup(title="Settings")
        notes = [getattr(self, f"{name}_rows")(self.group) for name in names]
        self.group.set_description(". ".join(note for note in notes if note) or None)


class SkillDialog(Adw.PreferencesDialog):
    """Everything about one skill: where it comes from, what it may do and why that is its risk,
    its settings, what the model is given for each action, word for word, and its manifest.

    What a skill says about itself is a stranger's text, so every row shows it as it is, never
    as markup. The words around it are Tuxistant's own."""

    def __init__(self, win, skill):
        super().__init__(search_enabled=False, content_width=680, content_height=760)
        self.win, self.skill = win, skill
        self.details = None     # SkillDetails, once it answers
        self.problem = ""       # why there are none, if it failed
        self.about_rows, self.permission_rows, self.manifest_row = [], [], None
        self.set_title(shown(skill["name"]))
        self.page = Adw.PreferencesPage()
        self.add(self.page)
        names = SKILL_SETTINGS.get(skill["id"], ()) if skill.get("source") != "yours" else ()
        self.settings = SkillSettings(win, names) if names else None
        self.about = Adw.PreferencesGroup()
        self.page.add(self.about)
        if self.settings:
            self.page.add(self.settings.group)
            self.settings.sync(win.config)
            self.settings.load()
        self.permissions = Adw.PreferencesGroup(title="Permissions")
        self.page.add(self.permissions)
        self.actions = Adw.PreferencesGroup(
            title="What the model is given",
            description="For each action, the tool name, description and parameters the model "
                        "reads, exactly as Tuxistant sends them")
        self.page.add(self.actions)
        self.manifest = Adw.PreferencesGroup(title="Manifest", visible=False)
        self.page.add(self.manifest)
        self.files = Adw.PreferencesGroup(title="Files", visible=False)
        self.page.add(self.files)
        self.waiting = Adw.ActionRow(title="Loading…")
        self.waiting.add_prefix(Adw.Spinner())
        self.actions.add(self.waiting)
        self.show_about(skill)
        self.show_permissions(skill, None)
        win.daemon.fetch("SkillDetails", self.fill, (skill["id"],), "s", fail=self.failed)

    def sync(self, config):
        if self.settings:
            self.settings.sync(config)

    def load(self):
        if self.settings:
            self.settings.load()

    def show_about(self, skill):
        for row in self.about_rows:
            self.about.remove(row)
        where = skill_origin(skill)
        if skill.get("source") == "yours" and skill.get("file"):
            where += f"\n{path_shown(skill['file'])}"
        rows = [detail_row("What it does", skill["description"]),
                detail_row("Where it comes from", where)]
        if skill.get("version"):
            rows.append(detail_row("Version", skill["version"]))
        if "manifest" in skill:
            rows.append(detail_row("Code", code_words(skill)))
        self.about_rows = rows
        for row in rows:
            self.about.add(row)

    def show_permissions(self, skill, because):
        for row in self.permission_rows:
            self.permissions.remove(row)
        rows = [plain_row(what, where) for what, where in permission_list(skill["permissions"])]
        if not rows:
            rows.append(plain_row("None", "It needs no special permissions"))
        risk = plain_row(risk_label(skill["risk"]),
                         risk_words(skill["risk"], because, skill["permissions"]))
        risk.add_prefix(Gtk.Image(icon_name="dialog-information-symbolic"))
        rows.append(risk)
        self.permission_rows = rows
        for row in rows:
            self.permissions.add(row)

    def fill(self, details):
        self.actions.remove(self.waiting)
        if not isinstance(details, dict):
            self.show_problem("This skill isn't loaded any more. Close this and look again")
            return
        self.details = details
        self.show_about(details)
        self.show_permissions(details, details.get("risk_because", []))
        for action in details["actions"]:
            self.actions.add(SkillActionRow(action))
        self.show_manifest(details)
        self.show_files(details)

    def failed(self, error):
        self.actions.remove(self.waiting)
        if error.matches(Gio.DBusError.quark(), Gio.DBusError.UNKNOWN_METHOD):
            self.show_problem("The Tuxistant running now is older than these Settings. Restart "
                              "it to read what the model is given and the manifest")
        else:
            self.show_problem(f"Tuxistant didn't send this skill's details: "
                              f"{error_text(error.message)}")

    def show_problem(self, text):
        """What ListSkills said is still worth showing: the actions by name."""
        self.problem = text
        # A group's description is read as markup, and the error came from elsewhere.
        self.actions.set_description(GLib.markup_escape_text(shown(text)))
        for name in self.skill.get("actions", []):
            self.actions.add(plain_row(name, ""))

    def show_manifest(self, details):
        text = details.get("manifest") or ""
        path = details.get("file") or ""
        if not text:
            return
        copy = Gtk.Button(label="Copy", valign=Gtk.Align.CENTER)
        copy.connect("clicked", lambda _button: self.copy_manifest())
        self.manifest.set_header_suffix(copy)
        self.manifest.set_description(
            "What the skill says about itself, as it was loaded. It is read-only here"
            if details.get("source") != "yours" else
            "What the skill says about itself, as it was loaded. Change it in its file")
        read = plain_row(path_shown(Path(path).name or "Manifest"),
                         f"{manifest_size(text)}\n{path_shown(path)}", activatable=True,
                         subtitle_selectable=True)
        read.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
        read.connect("activated", lambda _row: self.push_subpage(ManifestPage(self, details)))
        self.manifest_row = read
        self.manifest.add(read)
        if details.get("recipe"):
            self.manifest.add(self.open_row("Open file", Path(path)))
        self.manifest.set_visible(True)

    def show_files(self, details):
        files = details.get("files") or []
        if not details.get("folder"):
            return
        self.files.add(detail_row("Folder", path_shown(details["folder"])))
        for name in files[:MAX_FILES_SHOWN]:
            row = plain_row(path_shown(name), "")
            row.add_css_class("raw")
            self.files.add(row)
        if len(files) > MAX_FILES_SHOWN:
            self.files.add(plain_row(f"And {len(files) - MAX_FILES_SHOWN} more files", ""))
        if details.get("source") == "yours":
            self.files.add(self.open_row("Open folder", Path(details["folder"])))
        self.files.set_visible(True)

    def open_row(self, title, path):
        row = Adw.ButtonRow(title=title, end_icon_name="external-link-symbolic")
        row.connect("activated", lambda _row: self.open_path(path))
        return row

    def open_path(self, path: Path):
        launcher = Gtk.FileLauncher.new(Gio.File.new_for_path(str(path)))
        launcher.launch(self.win, None, self.on_opened)

    def on_opened(self, launcher, result):
        try:
            launcher.launch_finish(result)
        except GLib.Error as e:
            if not e.matches(Gtk.DialogError.quark(), Gtk.DialogError.DISMISSED):
                self.add_toast(Adw.Toast(title=f"Could not open it: {e.message}",
                                         use_markup=False))

    def copy_manifest(self):
        self.get_clipboard().set_content(
            Gdk.ContentProvider.new_for_value(self.details["manifest"]))
        self.add_toast(Adw.Toast(title="Copied the manifest", use_markup=False))


class SkillActionRow(Adw.ExpanderRow):
    """One action, as the model is given it. Built on opening: a page can hold many."""

    def __init__(self, action):
        super().__init__(use_markup=False)
        self.action, self.built = action, False
        self.set_title(shown(action["tool"]["name"]))
        self.set_subtitle(shown(f"{action['name']}\n{held_back_words(action)}"))
        self.connect("notify::expanded", self.on_expanded)

    def on_expanded(self, *_):
        if not self.get_expanded() or self.built:
            return
        self.built = True
        tool = self.action["tool"]
        self.add_row(detail_row("Description", tool["description"]))
        parameters = tool.get("parameters") or {}
        required = parameters.get("required") or []
        for name, schema in (parameters.get("properties") or {}).items():
            self.add_row(detail_row(*param_words(name, schema, required)))
        if self.action.get("module"):
            self.add_row(detail_row("Code", module_file(self.action["module"])))
        self.add_row(detail_row(SENT_AS, json.dumps(tool, indent=2, ensure_ascii=False)))


def colour_text(buffer, tags, spans):
    """Tags each (start, end, kind) in order, walking one iterator forward. Looking each place up
    from the start costs more for every tag already on its line."""
    at, place = buffer.get_start_iter(), 0
    for start, end, kind in spans:
        at.forward_chars(start - place)
        stop = at.copy()
        stop.forward_chars(end - start)
        buffer.apply_tag(tags[kind], at, stop)
        at, place = stop, end


class ManifestPage(Adw.NavigationPage):
    """The whole manifest, read-only, to read, select and copy."""

    def __init__(self, dialog, details):
        super().__init__(title=path_shown(Path(details.get("file") or "manifest").name))
        copy = Gtk.Button(label="Copy")
        copy.connect("clicked", lambda _button: dialog.copy_manifest())
        header = Adw.HeaderBar()
        header.pack_end(copy)
        self.text = Gtk.TextView(
            editable=False, cursor_visible=False, monospace=True,
            wrap_mode=Gtk.WrapMode.WORD_CHAR, top_margin=12, bottom_margin=12, left_margin=12,
            right_margin=12)
        self.text.update_property([Gtk.AccessibleProperty.LABEL], ["Manifest"])
        text = shown(details["manifest"])
        if max(map(len, text.split("\n"))) > LONG_LINE:
            self.text.set_wrap_mode(Gtk.WrapMode.CHAR)
        buffer = self.text.get_buffer()
        buffer.set_text(text)
        self.tags = {kind: buffer.create_tag(kind) for kind in highlight.KINDS}
        self.tags[highlight.COMMENT].props.style = Pango.Style.ITALIC
        colour_text(buffer, self.tags, highlight.shown_spans(details["manifest"]))
        self.watching = []
        # On the page, not the view: a bound method of the page held by its own child is a
        # cycle PyGObject can't collect, and a page popped would never be freed.
        self.connect("realize", self.watch_style)
        self.connect("unrealize", self.unwatch_style)
        view = Adw.ToolbarView(content=Gtk.ScrolledWindow(
            child=self.text, hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True))
        view.add_top_bar(header)
        self.set_child(view)

    def watch_style(self, _page):
        """Colours follow light and dark as they change. Under high contrast the text keeps the
        theme's own colour, and keys are bold instead."""
        style = Adw.StyleManager.get_default()
        self.watching = [style.connect(f"notify::{name}", self.colour)
                         for name in ("dark", "high-contrast")]
        self.colour(style)

    def unwatch_style(self, _page):
        style = Adw.StyleManager.get_default()
        for handler in self.watching:
            style.disconnect(handler)
        self.watching = []

    def colour(self, style, *_args):
        plain = style.get_high_contrast()
        for kind, tag in self.tags.items():
            tag.props.foreground = SYNTAX[kind][style.get_dark()]
            tag.props.foreground_set = not plain
        self.tags[highlight.KEY].props.weight = Pango.Weight.BOLD if plain else Pango.Weight.NORMAL


class SettingsOnlyDialog(Adw.PreferencesDialog):
    """Settings whose skill isn't loaded, so they can still be changed or removed."""

    def __init__(self, win, name):
        super().__init__(search_enabled=False, content_width=680, content_height=-1)
        self.skill, self.name = None, name
        self.set_title(SETTINGS_TITLES[name])
        self.settings = SkillSettings(win, (name,))
        note = self.settings.group.get_description()
        self.settings.group.set_description(f"{NOT_LOADED}. {note}" if note else NOT_LOADED)
        page = Adw.PreferencesPage()
        page.add(self.settings.group)
        self.add(page)
        self.settings.sync(win.config)
        self.settings.load()

    def sync(self, config):
        self.settings.sync(config)

    def load(self):
        self.settings.load()


def unreachable_settings(skills) -> list[str]:
    """The rows of settings that no listed skill's page shows, in a fixed order."""
    shown_now = {name for skill in skills if skill.get("source") != "yours"
                 for name in SKILL_SETTINGS.get(skill.get("id"), ())}
    return [name for name in SETTINGS_TITLES if name not in shown_now]


class SkillsPage(Adw.PreferencesPage):
    def __init__(self, win):
        super().__init__()
        self.win = win
        self.rows = []
        self.problem_rows = []
        self.dialog = None      # the skill page open now, if any
        open_skills = Gtk.Button(label="Open skills folder", valign=Gtk.Align.CENTER)
        open_skills.connect("clicked", self.on_open_folder)
        self.group = Adw.PreferencesGroup(
            title="Skills", header_suffix=open_skills,
            description="What Tux can do for you. Open one to read it and change its settings. "
                        "Risk follows from what a skill is allowed to do")
        self.problems = Adw.PreferencesGroup(
            title="Skills with problems",
            description="These skills can't be used until they are fixed", visible=False)
        self.orphans = Adw.PreferencesGroup(
            title="Settings of skills that aren't loaded",
            description="Kept for when the skill loads again, and here to change or remove",
            visible=False)
        self.orphan_rows = []
        self.add(self.group)
        self.add(self.problems)
        self.add(self.orphans)

    def load(self):
        self.win.daemon.fetch("ListSkills", self.fill)
        self.win.daemon.fetch("SkillProblems", self.fill_problems, fail=lambda _error: None)
        if self.dialog:
            self.dialog.load()

    def fill(self, skills):
        for row in self.rows:
            self.group.remove(row)
        self.rows = [self.make_row(skill) for skill in skills]
        for row in self.rows:
            self.group.add(row)
        for row in self.orphan_rows:
            self.orphans.remove(row)
        self.orphan_rows = []
        for name in unreachable_settings(skills):
            row = plain_row(SETTINGS_TITLES[name], "Its skill isn't loaded", activatable=True)
            row.settings_name = name
            row.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
            row.connect("activated", lambda row: self.open_settings(row.settings_name))
            self.orphan_rows.append(row)
            self.orphans.add(row)
        self.orphans.set_visible(bool(self.orphan_rows))

    def fill_problems(self, problems):
        for row in self.problem_rows:
            self.problems.remove(row)
        self.problem_rows = []
        for title, problem, path in skill_problems(problems):
            row = plain_row(title, problem, tooltip_text=path)
            row.add_prefix(Gtk.Image(icon_name="dialog-warning-symbolic", css_classes=["warning"]))
            self.problem_rows.append(row)
            self.problems.add(row)
        self.problems.set_visible(bool(self.problem_rows))

    def on_open_folder(self, _button):
        open_folder(self.win, SKILLS_FOLDER, "skills", self.on_folder_opened)

    def on_folder_opened(self, launcher, result):
        folder_opened(self.win, "skills", launcher, result)

    def make_row(self, skill):
        row = SkillRow(skill)
        row.connect("activated", lambda row: self.open(row.skill))
        return row

    def open(self, skill):
        self.dialog = SkillDialog(self.win, skill)
        self.dialog.connect("closed", self.on_closed)
        self.dialog.present(self.win)
        return self.dialog

    def open_settings(self, name):
        self.dialog = SettingsOnlyDialog(self.win, name)
        self.dialog.connect("closed", self.on_closed)
        self.dialog.present(self.win)
        return self.dialog

    def on_closed(self, dialog):
        if self.dialog is dialog:
            self.dialog = None

    def sync(self, config):
        if self.dialog:
            self.dialog.sync(config)


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
        """On a skill's page when one is open: the window's own would be hidden behind it."""
        toast = Adw.Toast(use_markup=False)
        toast.set_title(message)
        dialog = self.get_visible_dialog()
        (dialog if isinstance(dialog, Adw.PreferencesDialog) else self.toasts).add_toast(toast)

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
        self.skills.sync(config)
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
        elif name == "SkillsChanged":
            self.skills.load()

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
        # Themes and skills dropped into their folders show up on coming back from the file
        # manager, and a phone on coming back from pairing it.
        if self.is_active() and self.state == "ready":
            self.general.load()
            self.appearance.load()
            self.skills.load()

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
