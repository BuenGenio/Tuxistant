"""Opening an installed app, a page of GNOME Settings or one of your own folders. Only a desktop
file from Gio's own list is ever started, with no files, addresses or arguments, and only a folder
GLib names is opened, so nothing the model writes reaches a command line. Names are matched here,
not by the model. App names are other people's text (a browser's 'install as app' uses the page
title), so every reply naming one is said by the app, never returned to the model.
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

from . import files
from .files import _tilde
from .gate import APP_SAYS, MAX_DETAIL_CHARS, Ask, Speak, shown

OPEN_TIMEOUT_S = 10
MAX_SAID = 200          # characters of the request looked at
MAX_NAME = 60           # an app's name as said or shown
MAX_CHOICES = 3
CLOSE_ENOUGH = 0.85     # difflib ratio for a misheard name
CLOSE_LONG = 5          # letters: "team" is one letter from "steam", so short words aren't guessed
LOOSE_TIER = 5          # keywords and close spellings, which a Settings page's other words beat
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
# Field codes gio launch fills with files or addresses, which Tux never passes. Some of the others
# put the entry's icon (%i), name (%c) or file (%k) into the command after the card was shown.
PLAIN_FIELDS = frozenset("fFuU%")
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
NOT_THAT_KIND = "The {name} on this computer isn't {kind}."
OPENING = "Opening {name}."
OPENING_PAGE = "Opening {title} in Settings."
OPENING_SETTINGS = "Opening Settings."
WONT_OPEN = "{name} wouldn't open."
NO_SETTINGS = "GNOME Settings isn't installed on this computer."
NOT_INSTALLED = "That recipe opens {id}, which isn't installed here."
HIDDEN_APP = ("That recipe opens {id}, which isn't in the app grid, such as a helper that runs in "
              "the background, so I left it alone.")
NO_PAGE_HERE = "Settings on this computer has no {title} page."
NO_SUCH_PAGE = ("Settings has no page called {name}. It has pages such as Bluetooth, display, "
                "sound, Wi-Fi, network and power.")
CHANGED = "That app's shortcut changed after the card was shown, so I've left it alone."
UNREADABLE = "I can't read that app's shortcut, so I've left it alone."
FILLS_IN = ("That app's shortcut adds to its command as it starts, so a card couldn't show what "
            "runs. I've left it alone.")
TOO_LONG = "That app's shortcut is too long for a card to show all of it, so I've left it alone."
CARD_WHY = {"home": "It was added in your home folder, not installed as a package, so it can run "
                    "any command.",
            "other": "It isn't from an installed package, so it can run any command."}
OVER_DBUS = "It may be started over D-Bus by a service of its own, not by this command."
IN_TERMINAL = "It runs in a terminal."
OPENING_FOLDER = "Opening your {title} folder."
NO_FOLDER = "Your {title} folder isn't set up on this computer."
FOLDER_GONE = "I can't find your {title} folder."
FOLDER_OUTSIDE = "Your {title} folder isn't inside your home folder, so I've left it closed."
FOLDER_HIDDEN = "Your {title} folder is inside a hidden folder, so I've left it closed."
FOLDER_WONT_OPEN = "Your {title} folder wouldn't open."


def _single(word: str) -> str:
    return word[:-1] if len(word) > 3 and word.endswith("s") and not word.endswith("ss") else word


def _without_filler(words: list[str]) -> list[str]:
    while words and (words[0] in FILLER or words[0] in VERBS):
        words = words[1:]
    return words


def _without(words: list[str], unwanted) -> list[str]:
    while len(words) > 1 and words[0] in unwanted:
        words = words[1:]
    return words


def _cut(word: str, affixes, front: bool) -> str:
    """word without the affixes on that end, as long as something is left: "打开我的文档" is 文档."""
    for affix in affixes:
        if len(word) > len(affix) and (word.startswith(affix) if front else word.endswith(affix)):
            return _cut(word[len(affix):] if front else word[:-len(affix)], affixes, front)
    return word


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
               "proxy", "internet"),
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
class Folder:
    key: str                # GLib.UserDirectory.DIRECTORY_<key>, or HOME for the home folder
    title: str              # as said back: "Opening your Downloads folder."
    words: frozenset        # what it is called, in _words() form


def _folder_row(key: str, title: str, *words: str) -> Folder:
    return Folder(key, title, frozenset(_words(word) for word in words))


HOME = "HOME"
# Your own folders, as GLib knows them, called what xdg-user-dirs and Files call them in the
# languages the site uses: English, Spanish, Portuguese, Chinese and Arabic, written from memory
# and not yet checked against their translations. The name a folder really has is matched too,
# so a French or a German one answers to its own name.
FOLDERS = (
    _folder_row(HOME, "home", "home", "carpeta personal", "pasta pessoal", "主文件夹", "主目录",
                "المنزل"),
    _folder_row("DESKTOP", "Desktop", "desktop", "escritorio", "área de trabalho",
                "ambiente de trabalho", "桌面", "سطح المكتب"),
    _folder_row("DOCUMENTS", "Documents", "documents", "documentos", "文档", "المستندات"),
    _folder_row("DOWNLOAD", "Downloads", "downloads", "descargas", "transferências", "下载",
                "التنزيلات"),
    _folder_row("MUSIC", "Music", "music", "música", "音乐", "الموسيقى"),
    _folder_row("PICTURES", "Pictures", "pictures", "imágenes", "imagens", "图片", "الصور"),
    _folder_row("VIDEOS", "Videos", "videos", "vídeos", "视频", "الفيديو"),
    _folder_row("TEMPLATES", "Templates", "templates", "plantillas", "modelos", "模板", "القوالب"),
    _folder_row("PUBLIC_SHARE", "Public", "public", "público", "公共", "العامة"),
)
# Round a folder's name, as in "my Downloads folder", "carpeta de descargas" or "下载文件夹". In
# _words() form, which has already taken English's "my" and "open" off the front.
FOLDER_TERMS = {"folder", "directory", "carpeta", "pasta", "مجلد"}
# Said in front of a name in Spanish, Portuguese and Arabic, as English's are in FILLER and VERBS,
# and in French and German, as Settings shows its pages in those too: "mi carpeta", "les écrans".
LEADING = {_single(word) for word in ("mi", "mis", "el", "la", "los", "las", "meu", "minha",
                                      "meus", "minhas", "o", "os", "as", "abre", "abrir", "abra",
                                      "افتح", "le", "les", "l", "der", "die", "das", "den", "dem")}
LINKS = {"de", "del", "do", "da", "dos", "das", "of", "for", "du", "des", "d", "von", "fur"}
CJK_LEADING = ("打开", "我的")      # Chinese writes them with no space: "打开我的下载"
CJK_FOLDER = ("文件夹", "目录")
# Round a page's name: a word for settings or for a page, in the languages the site uses, as in
# "la configuración del sonido", "o painel de som", "إعدادات الصوت" or "声音设置". What Settings
# itself is called on this computer counts as well, so "Einstellungen für Klang" is Klang.
PAGE_TERMS = frozenset(_words(word) for word in (
    "settings", "preferences", "options", "page", "panel", "configuración", "configuraciones",
    "ajustes", "preferencias", "opciones", "página", "configurações", "configuração",
    "definições", "preferências", "opções", "painel", "إعدادات", "صفحة"))
CJK_PAGE = ("设置", "页面", "面板")
# Arabic's article: a plain alif and a lam, joined to a word of two letters or more.
ARTICLE = re.compile(r"(?<![^\W_])\u0627\u0644(?=[^\W_]{2})")
FOLD = str.maketrans({"ł": "l", "ø": "o", "đ": "d", "ð": "d", "ħ": "h", "ı": "i", "ŀ": "l",
                      "æ": "ae", "œ": "oe", "þ": "th"})     # letters NFKD leaves whole


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
    # "Flatpak the Room": the rest takes the form the names have, with no "the" in front.
    if len(words) > 1 and words[0] in KINDS:
        return " ".join(_without_filler(words[1:])), KINDS[words[0]]
    if len(words) > 1 and words[-1] in KINDS:
        return " ".join(_without_filler(words[:-1])), KINDS[words[-1]]
    return query, None


def _form_words(text: str) -> list[str]:
    """_words() with the letters NFKD leaves whole folded, and Arabic's article off each word.
    The article is found while a hamza is still on its letter, as _words() makes أ, إ and آ a
    plain alif, and "ألوان" has none."""
    text = "".join(c for c in unicodedata.normalize("NFKC", str(text)[:MAX_SAID])
                   if not unicodedata.combining(c))
    return _words(ARTICLE.sub("", text)).translate(FOLD).split()


def _unframed(words: list[str], terms) -> list[str]:
    """words without articles in front, or a settings or page word in front or behind, with
    "de" or "of" between it and the name. Something is always left."""
    while True:
        words = _without(words, LEADING)
        for term in terms:
            if len(words) > len(term) and tuple(words[:len(term)]) == term:
                words = _without(words[len(term):], LINKS)
                break
            if len(words) > len(term) and tuple(words[-len(term):]) == term:
                words = words[:-len(term)]
                break
        else:
            return words


def _loose(text: str, terms) -> str:
    """One form for a page's name as said and as written: "la configuración del sonido" meets
    Sonido, "声音设置" meets 声音, "Wi Fi" meets "Wi-Fi" and "الصوت" meets "صوت"."""
    words = _form_words(text)
    if words:
        words[0] = _cut(words[0], CJK_LEADING, front=True)
        words[-1] = _cut(words[-1], CJK_PAGE, front=False)
    return "".join(_unframed(words, terms))


def _page_terms(settings: App | None) -> list[tuple]:
    names = settings.names if settings else ()
    return sorted({tuple(_form_words(term)) for term in (*PAGE_TERMS, *names)} - {()})


def _page_named(entries: list[App], *said: str) -> Panel | None:
    """A page's own title, or the name its desktop file shows, translated or not, matched
    loosely: case, accents, spaces, a plural s, an article, and a settings or page word."""
    by_id = {entry.id: entry for entry in entries}
    terms = _page_terms(by_id.get(SETTINGS_ID))
    wanted = {form for form in (_loose(text, terms) for text in said) if form}
    for panel in PANELS:
        names = (panel.title, *(by_id[panel.desktop].names if panel.desktop in by_id else ()))
        if wanted & {_loose(name, terms) for name in names}:
            return panel
    return None


def _page_word(query: str) -> Panel | None:
    squashed = _squash(query)
    return next((panel for panel in PANELS
                 if squashed in {_squash(word) for word in panel.words}), None)


def _page_inside(query: str) -> Panel | None:
    inside = [(len(word), panel) for panel in PANELS for word in panel.words
              if f" {word} " in f" {query} "]
    return max(inside, key=lambda found: found[0], default=(0, None))[1]


def _page(query: str, entries: list[App], settings: bool, tier: int | None,
          said: str = "") -> Panel | None:
    """With "settings" said, any page the words name. Otherwise an app named exactly this wins
    ("Terminal", "Clocks"), then a page named exactly this ("Bluetooth" is the page, not Blueman),
    then an app with the word in its name, generic name, id or program ("zoom" is Zoom Workplace,
    not Accessibility), then a page's other words. A page's name is also looked for in the words
    just as they came, as they can hold a settings word ("Ratón y panel táctil"), and _words()
    drops the hamza that tells "ألوان" from a word with the article."""
    if settings:
        return (_page_named(entries, query, said) or _page_word(query)
                or _page_inside(query))
    if tier == 0:
        return None
    if named := _page_named(entries, query, said):
        return named
    return _page_word(query) if tier is None or tier >= LOOSE_TIER else None


def _close(said: str, entries: list[App]) -> tuple[int | None, list[App]]:
    if len(_squash(said)) < CLOSE_LONG:
        return None, []

    def score(entry: App) -> float:
        forms = {_squash(name) for name in entry.names}
        forms |= {word for name in entry.names for word in name.split()}
        return max((_closeness(said, form) for form in forms), default=0.0)

    scored = [(score(entry), entry) for entry in entries]
    found = [entry for value, entry in sorted(scored, key=lambda pair: -pair[0])
             if value >= CLOSE_ENOUGH]
    return (6, found) if found else (None, [])


def _closeness(said: str, form: str) -> float:
    """Every word said must mostly line up with the name, or "ms team" would be "steam"."""
    matcher = difflib.SequenceMatcher(None, _squash(said), form)
    matched = {at for block in matcher.get_matching_blocks()
               for at in range(block.a, block.a + block.size)}
    start = 0
    for word in said.split():
        if 2 * len(matched & set(range(start, start + len(word)))) <= len(word):
            return 0.0
        start += len(word)
    return matcher.ratio()


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
    return _close(query, shown_ones)


def _matching_kind(query: str, entries: list[App]) -> tuple[int | None, list[App]]:
    """"Flatpak Firefox" looks among the Flatpaks first, and at everything if none fits. An app
    named exactly what was said comes first, as "System Monitor" is not a system "monitor". The
    tier is always the whole query's, as a Settings page is matched against all that was said:
    "system details" is About, not an app called Device Details."""
    whole = _matching(query, entries)
    rest, kind = _kind_asked(query)
    if kind and whole[0] != 0:
        _, found = _matching(rest, [entry for entry in entries if entry.kind == kind])
        if found:
            return whole[0], found
    return whole


def _named_exactly(said: str, entries: list[App]) -> tuple[list[App], str | None]:
    """Apps named exactly what was said, settings word and all, with a kind word in front or
    behind if one was said. "System settings" stays the page, not Settings from the system.
    When only other kinds have that name, they come back with the kind asked, so a wrong kind
    word is answered instead of falling through to a page."""
    tier, found = _matching(said, entries)
    rest, kind = _kind_asked(_without_tail(said))
    if tier == 0 or not (kind and _settings_asked(rest)[0]):
        return (found if tier == 0 else []), None
    tier, found = _matching(rest, [entry for entry in entries if entry.kind == kind])
    if tier == 0:
        return found, None
    tier, found = _matching(rest, entries)
    return (found, kind) if tier == 0 else ([], None)


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
    # "draw.io" and "drawio" answer to the same words, so asking which one would never end.
    groups: dict[str, list[App]] = {}
    for app in found:
        groups.setdefault(_squash(_words(app.name)) or app.name, []).append(app)
    names = [group[0].name for group in groups.values()]
    if len(names) == 1:
        return _twins(next(iter(groups.values())), config, session)
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
    if _again(app.id):    # the 4B's second copy of a call whose card was just allowed
        return _say(session, config, text)
    # The card is made from the very bytes whose digest is checked after the click.
    data = _read(app.path)
    keys = _keyfile(data) if data is not None else None
    if keys is None:
        return _say(session, config, UNREADABLE)
    if _fills_in(_value(keys, "Exec")):
        return _say(session, config, FILLS_IN)
    # A card that cuts would hide the end of the command, which is where a surprise would be.
    if len(detail := _card(app, keys)) > MAX_DETAIL_CHARS:
        return _say(session, config, TOO_LONG)
    digest = hashlib.sha256(data).hexdigest()

    def run() -> dict:
        if _digest(app.path) != digest:
            return _say(session, config, CHANGED)
        return _launch_and_say(app, text, config, session)

    return Ask(f"Open {app.name}?", detail, run)


def _card(app: App, keys) -> str:
    lines = [CARD_WHY[app.kind]]
    if command := _value(keys, "Exec"):
        lines.append(f"Runs: {shown(command, MAX_DETAIL_CHARS)}")
    if _flag(keys, "DBusActivatable"):
        lines.append(OVER_DBUS)
    if _flag(keys, "Terminal"):
        lines.append(IN_TERMINAL)
    if folder := _value(keys, "Path"):
        lines.append(f"In folder: {shown(folder, MAX_DETAIL_CHARS)}")
    lines.append(f"File: {shown(_tilde(Path(app.path)), MAX_DETAIL_CHARS)}")
    return "\n".join(lines)


def _fills_in(command: str) -> bool:
    """Read left to right as GLib does, so "%%i" is a percent sign and an i."""
    return any(code not in PLAIN_FIELDS for code in re.findall(r"%(.)", command, re.DOTALL))


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
    return _opened(app.id, lambda: _launch(app.path), text, WONT_OPEN.format(name=app.name),
                   config, session)


def _opened(key: str, launch, text: str, failed: str, config, session) -> dict:
    if not _again(key):
        if not launch():
            return _say(session, config, failed)
        _remember(key)
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


def _folder_paths() -> dict[str, str | None]:
    """Your folders where GLib says they are, by FOLDERS' keys: None for one that isn't set."""
    GLib = _glib()
    paths = {HOME: GLib.get_home_dir()}
    for folder in FOLDERS:
        if folder.key != HOME:
            paths[folder.key] = GLib.get_user_special_dir(
                getattr(GLib.UserDirectory, f"DIRECTORY_{folder.key}"))
    return paths


def _set_up(folder: Folder, paths: dict) -> str | None:
    """A folder set to the home folder isn't used, as xdg-user-dirs has it, and nor is one that
    is the home folder by a link or a "..": it isn't a folder of its own."""
    path, home = paths.get(folder.key), paths.get(HOME)
    if not path or (folder.key != HOME and home
                    and os.path.realpath(path) == os.path.realpath(home)):
        return None
    return path


def _checked(path: Path) -> Path:
    """files._checked, but a folder really outside the home folder is called that even when its
    path goes through "..", which files._checked would call a hidden folder: "$HOME/../other"."""
    if files._inside(path.resolve()) is None:
        raise ValueError(files.OUTSIDE)
    return files._checked(path)


def _framed(words: list[str]) -> tuple[list[str], bool]:
    """words with a folder term taken off either end, and whether there was one."""
    called = False
    if words and (last := _cut(words[-1], CJK_FOLDER, front=False)) != words[-1]:
        words, called = [*words[:-1], last], True
    if len(words) > 1 and words[-1] in FOLDER_TERMS:
        words, called = words[:-1], True
    if len(words) > 1 and words[0] in FOLDER_TERMS:
        words, called = _without(words[1:], LINKS), True
    return words, called


def _folder_called(said: str, paths: dict) -> Folder | None:
    squashed = _squash(said)
    if not squashed:
        return None
    for folder in FOLDERS:
        if squashed in {_squash(word) for word in folder.words}:
            return folder
    for folder in FOLDERS:      # the home folder's own name is yours, not a name for it
        path = _set_up(folder, paths) if folder.key != HOME else None
        if path and squashed == _squash(_words(Path(path).name)):
            return folder
    return None


def _folder_asked(query: str, paths: dict) -> tuple[Folder | None, bool]:
    """Which of your folders was named, and whether it was called a folder: "downloads", "my
    Downloads folder", "carpeta de descargas", "下载文件夹", "مجلد التنزيلات"."""
    whole = _without(query.split(), LEADING)
    if whole:
        whole = [_cut(whole[0], CJK_LEADING, front=True), *whole[1:]]
    bare, called = _framed(whole)       # "carpeta personal" is called a folder too
    for said in (" ".join(whole), " ".join(bare)):
        if folder := _folder_called(said, paths):
            return folder, called
    return None, False


def _open_folder(folder: Folder, paths: dict, config, session) -> dict:
    """As file.open opens a folder, with gio open and no card: inside the home folder and outside
    hidden ones, links followed. The home folder itself, which file.open refuses, opens where
    GLib says it is. Only the path GLib gave is opened, never anything said."""
    path = _set_up(folder, paths)
    if path is None:
        return _say(session, config, NO_FOLDER.format(title=folder.title))
    try:
        real = Path(path).resolve() if folder.key == HOME else _checked(Path(path))
    except ValueError as e:
        refusal = {files.OUTSIDE: FOLDER_OUTSIDE, files.HIDDEN: FOLDER_HIDDEN}.get(str(e),
                                                                                  FOLDER_GONE)
        return _say(session, config, refusal.format(title=folder.title))
    except (OSError, RuntimeError):
        return _say(session, config, FOLDER_GONE.format(title=folder.title))
    if not real.is_dir():
        return _say(session, config, FOLDER_GONE.format(title=folder.title))
    return _opened(f"folder:{real}", lambda: files._launch(real),
                   OPENING_FOLDER.format(title=folder.title),
                   FOLDER_WONT_OPEN.format(title=folder.title), config, session)


def open_app(args: dict, config, session):
    said = _words(args.get("name") or "")
    # "Default apps" is a page's word, though "apps" is usually only the end of an app's name.
    query, settings = _settings_asked(said if _page_word(said) else _without_tail(said))
    if not query and not settings:
        raise ValueError(NO_NAME)
    # Only the folders GLib names, so a path the model wrote never reaches the opener. Never
    # with "settings" or "app" said: "the music app" is Rhythmbox Music Player.
    paths = _folder_paths()
    asked_app = _without_tail(said) != said
    folder, called = (None, False) if settings or asked_app else _folder_asked(query, paths)
    # "Folder" said: the folder, even beside an app named exactly that, as a web page installed
    # as an app could name itself "Downloads Folder".
    if folder and called:
        return _open_folder(folder, paths, config, session)
    entries = _installed()
    if not query or (settings and query == "gnome"):     # "GNOME Settings", "GNOME Control Center"
        return _open_by_id(SETTINGS_ID, OPENING_SETTINGS, entries, config, session)
    # "KDE System Settings" is an app, though its settings word would make it the System page.
    exact, wrong_kind = _named_exactly(said, entries) if settings else ([], None)
    if wrong_kind:
        return _say(session, config, NOT_THAT_KIND.format(name=exact[0].name,
                                                          kind=KIND_SAID[wrong_kind]))
    if exact:
        return _open_found(exact, config, session)
    # "Firefox settings" opens Firefox, as Settings has no page for it.
    tier, found = _matching_kind(query, entries)
    # An app with the name, its generic name or the word in its name, such as GNOME's Music or
    # Rhythmbox Music Player, wins as in 0.9.0. The folder beats keywords and close spellings,
    # as a Settings page's other words do.
    if folder and (tier is None or tier >= LOOSE_TIER):
        return _open_folder(folder, paths, config, session)
    if panel := _page(query, entries, settings, tier, str(args.get("name") or "")):
        return _open_panel(panel, entries, config, session)
    if found:
        return _open_found(found, config, session)
    if settings:
        return _say(session, config, NO_SUCH_PAGE.format(name=_said(query)))
    # Only the user's words, so the model can still try file_find for a folder.
    raise ValueError(NOT_FOUND.format(name=_said(query)))


def open_entry(app_id: str, page: str, config, session):
    """What a recipe opens: one id or one page its author wrote, never a name the model said.
    The same rules as app.open from there, card and all."""
    entries = _installed()
    if page:
        panel = next(p for p in PANELS if p.desktop == f"gnome-{page}-panel.desktop")
        return _open_panel(panel, entries, config, session)
    for entry in entries:
        if entry.id in (app_id, f"{app_id}.desktop"):
            if not entry.shown:     # app.open can't reach these by name either
                return _say(session, config, HIDDEN_APP.format(id=_said(app_id)))
            return _start(entry, OPENING.format(name=entry.name), config, session)
    return _say(session, config, NOT_INSTALLED.format(id=_said(app_id)))


HANDLERS = {"app.open": open_app}
