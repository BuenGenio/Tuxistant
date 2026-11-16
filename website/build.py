#!/usr/bin/env python3
"""Builds the static site into dist/: one page per file in src/i18n/.

Adding a language means adding src/i18n/<code>.json with the same keys as en.json.
SITE_URL adds canonical and hreflang URLs; deploy.sh sets it. Links are relative, so the same pages
work at a domain's root and under /Tuxistant/ on GitHub Pages. Pages can't set response headers,
so the security policy is a meta tag in each page.

--preview UPCOMING.json builds a local preview of planned releases into dist-preview/ instead, with a
banner on every page. It is only for looking at: git ignores dist-preview/ and deploy.sh refuses it.
"""
import argparse
import base64
import hashlib
import html
import json
import os
import re
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE / "src"
DIST = HERE / "dist"
DIST_PREVIEW = HERE / "dist-preview"     # --preview: local only, git ignores it, deploy.sh refuses it
THEMES = HERE.parent / "themes"
SITE_URL = os.environ.get("SITE_URL", "").rstrip("/")
DONATE = "https://buymeacoffee.com/buengenio"
THEME_ORDER = ["tux", "orb", "waves", "scanner"]
OS_ORDER = [("linux", "Linux"), ("macos", "macOS"), ("windows", "Windows")]
MODES = ["auto", "light", "dark"]
# The "Works with" ticker, in the order shown: name, mark, status. One line each, so a brand is
# removed or moved by touching one line. A mark is a file in src/logos/ (SOURCES.md there says where
# each came from and on what terms); None sets the name in text instead. Statuses follow the rule
# for every claim on the site: now only once it has worked on a real machine.
WORKS_WITH = [
    ("Linux", "linux", "now"),
    ("GNOME", "gnome", "now"),
    ("BBC", "bbc", "now"),
    ("Tapo", "tapo", "now"),
    ("Open-Meteo", None, "now"),
    ("LibreTranslate", "libretranslate", "now"),
    ("The Kyiv Independent", "kyiv-independent", "now"),
    # Built, but not yet asked for by voice on a real machine, which is what earns "now".
    ("Google Calendar", "google-calendar", "soon"),
    ("Home Assistant", "home-assistant", "soon"),
    ("Spotify", "spotify", "soon"),
    ("InteliMaris", "intelimaris", "soon"),
    ("CNN", "cnn", "soon"),     # not one of the news sources yet
    ("Windows", "windows", "soon"),
    ("macOS", "macos", "soon"),
]
# Each mark: the height it is drawn at in px, so wordmarks and symbols carry similar weight, and
# whether it spells the name itself. A symbol gets its name set beside it, and so does a mark not
# listed here, at 22 px.
MARKS = {
    "linux": (28, False), "gnome": (24, False), "google-calendar": (22, False),
    "libretranslate": (24, False), "home-assistant": (23, False), "spotify": (22, False),
    "intelimaris": (26, False), "windows": (20, False),
    "bbc": (18, True), "kyiv-independent": (24, True), "tapo": (22, True), "cnn": (18, True),
    "macos": (13, True),
}
OURS = {"InteliMaris"}      # shown with its maker's leave; every other name is a trademark we only name
MODE_ICONS = (
    '<svg class="i-auto" viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="9"/>'
    '<path class="solid" d="M12 3a9 9 0 0 1 0 18z"/></svg>'
    '<svg class="i-light" viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="4"/>'
    '<path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4'
    'M17.7 6.3l1.4-1.4"/></svg>'
    '<svg class="i-dark" viewBox="0 0 24 24" aria-hidden="true">'
    '<path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/></svg>'
)
# Runs before first paint on every page, so a stored choice never flashes the system's colours.
MODE_SCRIPT = ("try{var m=localStorage.getItem('mode');"
               "if(m==='light'||m==='dark')document.documentElement.dataset.mode=m}catch(e){}")

esc = html.escape


def load_languages():
    languages = {p.stem: json.loads(p.read_text()) for p in sorted((SRC / "i18n").glob("*.json"))}
    base = set(languages["en"])
    for code, strings in languages.items():
        missing = base - set(strings)
        if missing:
            raise SystemExit(f"{code}.json is missing {sorted(missing)}")
    return dict(sorted(languages.items(), key=lambda item: item[0] != "en"))


def path_for(code):
    return "/" if code == "en" else f"/{code}/"


# Runs before first paint on / only: follows the browser unless the visitor chose a language.
def detect_script(codes):
    return (
        "(function(){var L=" + json.dumps(codes) + ",c=null,i,b;"
        "try{c=localStorage.getItem('lang')}catch(e){}"
        "if(L.indexOf(c)<0){c=null;var n=navigator.languages||[navigator.language||''];"
        "for(i=0;i<n.length&&!c;i++){b=n[i].toLowerCase().split('-')[0];if(L.indexOf(b)>=0)c=b}}"
        "if(c&&c!=='en')location.replace(c+'/'+location.search+location.hash)})()"
    )


def tag(strings, status):
    return f'<span class="tag tag-{status}">{esc(strings[status])}</span>'


def caps_html(strings):
    items = [f'<li class="cap">{tag(strings, status)}<span>{esc(text)}</span></li>'
             for status, text in strings["caps"]]
    half = len(items) // 2
    return "\n".join(items[:half]), "\n".join(items[half:])


def panels_html(strings):
    tabs, panels = [], []
    for os_id, os_name in OS_ORDER:
        tabs.append(f'<button class="tab" type="button" role="tab" id="tab-{os_id}" data-os="{os_id}" '
                    f'aria-controls="panel-{os_id}" aria-selected="false">{os_name}</button>')
        cards = []
        for example in strings["examples"][os_id]:
            status, say, does = example[:3]
            approve = (f'<span class="flag">{esc(strings["approve"])}</span>'
                       if len(example) > 3 else "")
            cards.append(
                f'<li class="example"><span class="say" data-label="{esc(strings["youSay"])}">'
                f'{esc(say)}</span><p class="does">{esc(does)}</p>'
                f'<div class="flags">{tag(strings, status)}{approve}</div></li>')
        panels.append(f'<ul class="examples" id="panel-{os_id}" role="tabpanel" '
                      f'aria-labelledby="tab-{os_id}"{"" if os_id == "linux" else " hidden"}>'
                      + "".join(cards) + "</ul>")
    return "".join(tabs), "".join(panels)


def mark_files():
    """The marks in use, each as (viewBox, drawing) from its file in src/logos/."""
    files = {}
    for mark in dict.fromkeys(mark for _, mark, _ in WORKS_WITH if mark):
        text = (SRC / "logos" / f"{mark}.svg").read_text()
        files[mark] = (re.search(r'viewBox="([^"]+)"', text)[1],
                       re.search(r"<svg[^>]*>(.*)</svg>", text, re.S)[1])
    return files


def ticker_html(strings):
    """A label and two copies of one list. The second copy is what makes the loop seamless, so it
    is hidden from assistive tech. Each mark is drawn once, in a sprite both copies point at."""
    files = mark_files()
    sprite = "".join(f'<symbol id="mark-{mark}" viewBox="{view}">{drawing}</symbol>'
                     for mark, (view, drawing) in files.items())
    items = []
    for name, mark, status in WORKS_WITH:
        picture, hidden = "", ""
        if mark:
            height, spells_name = MARKS.get(mark, (22, False))
            width, tall = map(float, files[mark][0].split()[2:])
            picture = (f'<svg class="mark" width="{height * width / tall:.4g}" height="{height}" '
                       f'aria-hidden="true" focusable="false"><use href="#mark-{mark}"/></svg>')
            hidden = " visually-hidden" if spells_name else ""
        items.append(f'<li class="ticker-item is-{status}">{picture}'
                     f'<span class="mark-name{hidden}">{esc(name)}</span>{tag(strings, status)}</li>')
    items = "".join(items)
    return (f'<svg class="marks" aria-hidden="true" focusable="false">{sprite}</svg>'
            f'<p class="ticker-label" id="works-with">{esc(strings["worksWith"])}</p>'
            '<div class="ticker-view">'
            f'<ul class="ticker-list" aria-labelledby="works-with" tabindex="0">{items}</ul>'
            f'<ul class="ticker-list" aria-hidden="true">{items}</ul></div>')


def versions_html(strings, releases):
    rows = []
    for os_id, os_name in OS_ORDER:
        release = releases[os_id]
        needs = strings["linuxNeeds"] if os_id == "linux" else strings["tbd"]
        rows.append(f'<tr><td>{os_name}</td><td>{esc(release["version"] or "–")}</td>'
                    f'<td>{esc(strings[release["channel"]])}</td>'
                    f'<td class="needs">{esc(needs)}</td></tr>')
    return "".join(rows)


def release_in(releases, version):
    return next(entry for entry in releases["history"] if entry["version"] == version)


def preview_html(strings, releases):
    """The secondary link under the main download: the newest preview, and that it is one. A planned
    preview (only in a local preview build) has nothing to link to yet, so it shows its date."""
    preview = releases["preview"]
    if not preview:     # null once a stable release is newer than any preview
        return ""
    text = esc(fill(strings["previewTry"], version=preview["version"]))
    if preview["url"]:
        line = (f'<a href="{esc(preview["url"])}">{text}</a>{esc(strings["previewSep"])}{esc(strings["previewUntried"])} · '
                f'<a href="{esc(preview["page"])}">{esc(strings["releaseNotes"])}</a>')
    else:
        line = f'{text} · {planned_html(strings, release_in(releases, preview["version"]))}'
    return f'<p class="dl-preview">{line}</p>'


def planned_html(strings, entry):
    return (f'<span class="chan chan-planned">{esc(strings["planned"])}</span> '
            f'<time dir="ltr" datetime="{entry["date"]}">{entry["date"]}</time>')


# The release chips' icons, drawn for this site like MODE_ICONS: 24 units square, a 2-unit round
# stroke and no fill, so each one takes the chip's colour. A feature in releases.json names one.
FEATURE_ICONS = {
    "mail": '<rect x="3" y="5" width="18" height="14" rx="2"/><path d="m3.5 7 8.5 6 8.5-6"/>',
    "search": '<circle cx="10.5" cy="10.5" r="6.5"/><path d="m15.5 15.5 5 5"/>',
    "home": '<path d="M4 10.5 12 4l8 6.5V20H4z"/><path d="M10 20v-5h4v5"/>',
    "timer": '<circle cx="12" cy="13.5" r="7.5"/><path d="M12 13.5V10M9.5 2.5h5M12 2.5V6"/>',
    "media": ('<path d="M9 17.5V5.5l11-2v12"/><circle cx="6.5" cy="17.5" r="2.5"/>'
              '<circle cx="17.5" cy="15.5" r="2.5"/>'),
    "book": ('<path d="M12 6.5C10 5 7 4.5 3 5v13.5c4-.5 7 0 9 1.5 2-1.5 5-2 9-1.5V5c-4-.5-7 0-9 1.5z"/>'
             '<path d="M12 6.5V20"/>'),
    "code": '<path d="m8 7-5 5 5 5M16 7l5 5-5 5M13.5 5l-3 14"/>',
    "recipe": ('<path d="M14 3H6.5A1.5 1.5 0 0 0 5 4.5v15A1.5 1.5 0 0 0 6.5 21h11a1.5 1.5 0 0 0 1.5-1.5V8z"/>'
               '<path d="M14 3v5h5M8.5 13h7M8.5 17h4"/>'),
    "followup": ('<path d="M4 3.5h10a1 1 0 0 1 1 1v7a1 1 0 0 1-1 1H8.5L5 15.5v-3H4a1 1 0 0 1-1-1v-7a1 1 0 0 1 1-1z"/>'
                 '<path d="M18 8h2a1 1 0 0 1 1 1v7a1 1 0 0 1-1 1h-1v3l-3.5-3H11a1 1 0 0 1-1-1v-.5"/>'),
    "dictation": '<path d="M4 7h9M4 12h6M4 17h8M18 5v14M15.5 5h5M15.5 19h5"/>',
    "voice": ('<rect x="9" y="3" width="6" height="11" rx="3"/>'
              '<path d="M5.5 11a6.5 6.5 0 0 0 13 0M12 17.5V21M9 21h6"/>'),
    "approve": '<rect x="3" y="5" width="18" height="14" rx="2"/><path d="m8.5 12 2.5 2.5 5-5"/>',
    "settings": ('<path d="M4 7h9M17 7h3M4 17h3M11 17h9"/><circle cx="15" cy="7" r="2"/>'
                 '<circle cx="9" cy="17" r="2"/>'),
    "theme": ('<path d="M12 3a9 9 0 0 0 0 18c1.4 0 2-1 1.5-2.2-.6-1.4.3-2.8 1.8-2.8H18a3 3 0 0 0 3-3'
              'c0-5.5-4-10-9-10z"/><path d="M7.5 11.5h.01M10 7.5h.01M15 7.5h.01"/>'),
    "apps": ('<rect x="4" y="4" width="6.5" height="6.5" rx="1.5"/><rect x="13.5" y="4" width="6.5" height="6.5" rx="1.5"/>'
             '<rect x="4" y="13.5" width="6.5" height="6.5" rx="1.5"/>'
             '<rect x="13.5" y="13.5" width="6.5" height="6.5" rx="1.5"/>'),
    "shield": '<path d="M12 3 5 6v5.5c0 4.5 3 8 7 9.5 4-1.5 7-5 7-9.5V6z"/><path d="M9.5 12.5h5"/>',
    "consent": ('<path d="M7 18.5a4.5 4.5 0 0 1-.6-9 6 6 0 0 1 11.4 1.6 3.8 3.8 0 0 1-.3 7.4z"/>'
                '<path d="m9.5 13.5 2 2 3.5-3.5"/>'),
    "calendar": '<rect x="3.5" y="5" width="17" height="15.5" rx="2"/><path d="M3.5 10h17M8 3v4M16 3v4"/>',
    "news": '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M7 8.5h10M7 12.5h4M7 16h4M14 12.5h3V16h-3z"/>',
}
# The icons with lines of writing, left-aligned on a page or a list, which run the other way right
# to left, so the Arabic page mirrors them.
FEATURE_MIRRORED = {"dictation", "news", "recipe"}
# Characters: a chip, not a sentence. About 40 Latin characters fit one line at 360 px.
FEATURE_LABEL_LENGTH = (2, 40)


def feature_sprite_html(releases):
    """Every icon the release chips use, drawn once in a sprite the chips point at. Its size is set
    here as well as in the stylesheet, so without the stylesheet it takes no room."""
    used = dict.fromkeys(feature["icon"] for entry in releases["history"] for feature in entry["features"])
    symbols = "".join(f'<symbol id="feat-{icon}" viewBox="0 0 24 24">{FEATURE_ICONS[icon]}</symbol>'
                      for icon in used)
    return f'<svg class="feat-sprite" width="0" height="0" aria-hidden="true" focusable="false">{symbols}</svg>'


def features_html(code, entry):
    """What a release added, one chip each, in the order releases.json gives: most impact first.
    role="list" because WebKit drops a list's role when it has no bullets."""
    chips = []
    for feature in entry["features"]:
        icon = feature["icon"]
        mirrored = " is-mirrored" if icon in FEATURE_MIRRORED else ""
        chips.append(f'<li class="feat"><svg class="feat-icon{mirrored}" width="15" height="15" '
                     f'aria-hidden="true" focusable="false"><use href="#feat-{icon}"/></svg>'
                     f'<span>{esc(feature["label"][code])}</span></li>')
    return f'<ul class="rel-added" role="list">{"".join(chips)}</ul>'


def releases_html(code, strings, releases):
    """Newest first: version, date, stable or preview, and a chip for each thing it added."""
    rows = []
    for entry in releases["history"]:
        version, date = esc(entry["version"]), entry["date"]
        if entry.get("planned"):
            version_html, when = version, planned_html(strings, entry)
        else:
            version_html = f'<a href="{esc(entry["page"])}">{version}</a>'
            when = f'<time dir="ltr" datetime="{date}">{date}</time>'
        rows.append(f'<li class="release{" is-planned" if entry.get("planned") else ""}">'
                    f'<span class="rel-version">{version_html}</span><span class="rel-when">{when}</span>'
                    f'<span class="chan chan-{entry["channel"]}">{esc(strings[entry["channel"]])}</span>'
                    f'{features_html(code, entry)}</li>')
    return "".join(rows)


def preview_caps_html(strings):
    return "".join(f'<li class="cap">{tag(strings, status)}<span>{esc(text)}</span></li>'
                   for status, text in strings["previewCaps"])


def local_preview_html(strings):
    """The banner on every page of a local preview build. The real site has none."""
    if "localPreview" not in strings:
        return ""
    return f'<div class="wrap"><p class="local-preview" role="note">{esc(strings["localPreview"])}</p></div>'


def fill(text, **values):
    return re.sub(r"\{(\w+)\}", lambda match: values[match[1]], text)


def version_key(version):
    return tuple(int(part) for part in version.split("."))


def check_releases(releases, languages):
    """releases.json drives the download, the preview link and the list, so a slip there stops
    the build instead of showing a wrong or dead link."""
    history = releases["history"]
    versions = [entry["version"] for entry in history]
    if versions != sorted(versions, key=version_key, reverse=True) or len(set(versions)) != len(versions):
        raise SystemExit("releases.json: history must list each version once, newest first")
    for entry in history:
        where = f"releases.json: {entry['version']}"
        if not re.fullmatch(r"\d{4}-\d\d-\d\d", entry["date"]):
            raise SystemExit(f"{where} needs a date as YYYY-MM-DD")
        if entry["channel"] not in ("stable", "preview"):
            raise SystemExit(f"{where} must be stable or preview")
        if bool(entry.get("planned")) == bool(entry.get("page")):
            raise SystemExit(f"{where} links its release page, or is planned and links nothing")
        check_features(where, entry, languages)
    linux, preview = releases["linux"]["version"], releases["preview"]
    if linux not in versions or release_in(releases, linux).get("planned"):
        raise SystemExit("releases.json: the main download must be a released version in history")
    if not preview:
        return
    if preview["version"] not in versions:
        raise SystemExit("releases.json: the preview must be in history")
    if version_key(preview["version"]) <= version_key(linux):
        raise SystemExit("releases.json: the preview must be newer than the main download, or null")
    if bool(preview["url"]) != bool(preview["page"]) or (
            bool(preview["url"]) == bool(release_in(releases, preview["version"]).get("planned"))):
        raise SystemExit("releases.json: a released preview has a download and a page, a planned one neither")


def check_features(where, entry, languages):
    """A release's chips: at least one, each with a known icon and a label in every language that
    starts with a capital where the script has capitals, and is short enough for a chip."""
    features = entry.get("features")
    if not isinstance(features, list) or not features:
        raise SystemExit(f"{where} lists what it added as features, most impact first")
    shortest, longest = FEATURE_LABEL_LENGTH
    for feature in features:
        if not isinstance(feature, dict):
            raise SystemExit(f"{where}: each feature is an icon and a label, not {feature!r}")
        label = feature.get("label") if isinstance(feature.get("label"), dict) else {}
        name = f"{where}: {label.get('en', feature)!r}"
        if not isinstance(feature.get("icon"), str) or feature["icon"] not in FEATURE_ICONS:
            raise SystemExit(f"{name} needs an icon from FEATURE_ICONS, not {feature.get('icon')!r}")
        if set(feature) != {"icon", "label"}:
            raise SystemExit(f"{name} has only an icon and a label, not {sorted(set(feature) - {'icon', 'label'})}")
        if set(label) != set(languages):
            raise SystemExit(f"{name} needs a label in every language and no other: missing "
                             f"{sorted(set(languages) - set(label))}, unknown {sorted(set(label) - set(languages))}")
        for code, text in label.items():
            if not isinstance(text, str) or text != text.strip() or not shortest <= len(text) <= longest:
                raise SystemExit(f"{name} in {code} needs {shortest} to {longest} characters, not {text!r}")
            if text[0].lower() != text[0].upper() and not text[0].isupper():
                raise SystemExit(f"{name} in {code} must start with a capital letter, not {text!r}")


def apply_upcoming(path, languages, releases):
    """A local preview of the site once the planned releases ship: their entries go on top of the
    history, the preview link names the newest, its claims join the preview's, and every page gets
    a banner. Claims stay Soon, since nothing planned has worked on a real machine."""
    upcoming = json.loads(Path(path).read_text())
    for entry in upcoming["releases"]:
        if not entry.get("planned"):
            raise SystemExit(f"{path}: {entry['version']} isn't out, so it is planned")
    releases["history"] = upcoming["releases"] + releases["history"]
    releases["preview"] = upcoming.get("preview", releases["preview"])
    for code, strings in languages.items():
        missing = [key for key in ("banner", "claims") if code not in upcoming[key]]
        if missing:
            raise SystemExit(f"{path} has no {' or '.join(missing)} in {code}")
        claims = upcoming["claims"][code]
        if any(status != "soon" for status, _ in claims):
            raise SystemExit(f"{path}: every claim in {code} is soon until it has worked on a real machine")
        strings["previewCaps"] = strings["previewCaps"] + claims
        strings.update(upcoming.get("strings", {}).get(code, {}))
        strings["localPreview"] = upcoming["banner"][code]


def page(code, strings, languages, releases, detect, policy):
    caps_a, caps_b = caps_html(strings)
    tabs, panels = panels_html(strings)
    options = "".join(
        f'<option value="{c}"{" selected" if c == code else ""}>{esc(s["name"])}</option>'
        for c, s in languages.items())
    modes = "".join(f'<option value="{m}">{esc(strings["modes"][m])}</option>' for m in MODES)
    alternates = ""
    if SITE_URL:
        alternates = "".join(f'<link rel="alternate" hreflang="{c}" href="{SITE_URL}{path_for(c)}">'
                             for c in languages)
        alternates += (f'<link rel="alternate" hreflang="x-default" href="{SITE_URL}/">'
                       f'<link rel="canonical" href="{SITE_URL}{path_for(code)}">')
    data = {k: strings[k] for k in ("dlFor", "dlSoon", "dlPending", "dlMeta", "dlPlanned",
                                    "preview", "stable", "planned", "themes", "showTheme")}
    data["releases"] = {os_id: releases[os_id] for os_id, _ in OS_ORDER}
    data_json = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    root = "" if code == "en" else "../"
    head_script = f"<script>{MODE_SCRIPT}</script>"
    if code == "en":
        head_script += f"<script>{detect}</script>"
    return f"""<!doctype html>
<html lang="{code}" dir="{strings['dir']}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="{policy}">
<meta name="referrer" content="strict-origin-when-cross-origin">
{head_script}
<title>{esc(strings['title'])}</title>
<meta name="description" content="{esc(strings['description'])}">
<meta property="og:title" content="{esc(strings['title'])}">
<meta property="og:description" content="{esc(strings['description'])}">
<meta name="theme-color" content="#07080d">
<link rel="icon" href="{root}favicon.svg" type="image/svg+xml">
<link rel="stylesheet" href="{root}style.css">
{alternates}
</head>
<body>
<header class="site-header" id="site-header">
  <div class="nav-glass" aria-hidden="true"></div>
  <div class="wrap bar">
    <a class="brand" href="./">Tuxistant</a>
    <select class="lang" id="lang" data-root="{root}" aria-label="{esc(strings['language'])}">{options}</select>
    <span class="mode">{MODE_ICONS}<select id="mode" aria-label="{esc(strings['appearance'])}" title="{esc(strings['appearance'])}">{modes}</select></span>
    <a class="btn btn-coffee" href="{DONATE}" rel="noopener">{esc(strings['donate'])}</a>
  </div>
</header>
<main>{local_preview_html(strings)}
  <section class="hero wrap">
    <h1>Tuxistant</h1>
    <p class="tagline">{esc(strings['tagline'])}</p>
    <div class="stage">
      <canvas id="tux" aria-hidden="true"></canvas>
      <div class="fallback" aria-hidden="true"></div>
      <ul class="caps caps-a">{caps_a}</ul>
      <ul class="caps caps-b">{caps_b}</ul>
    </div>
    <div class="pick"><span id="theme-name" aria-live="polite"></span><span class="dots" id="dots"></span></div>
    <div class="download">
      <a class="btn btn-main" id="download" aria-disabled="true"></a>
      <p class="dl-meta"><span id="dl-meta-text"></span> · <a href="#versions">{esc(strings['dlAll'])}</a></p>
      {preview_html(strings, releases)}
    </div>
  </section>
  <div class="wrap"><div class="ticker">{ticker_html(strings)}</div></div>
  <section class="section wrap" aria-labelledby="examples-title">
    <h2 id="examples-title">{esc(strings['examplesTitle'])}</h2>
    <div class="tabs" role="tablist" aria-labelledby="examples-title">{tabs}</div>
    {panels}
  </section>
  <section class="section wrap" id="preview" aria-labelledby="preview-title">
    <h2 id="preview-title">{esc(strings['previewTitle'])}</h2>
    <p class="lead">{esc(strings['previewLead'])}</p>
    <ul class="caps caps-row">{preview_caps_html(strings)}</ul>
  </section>
  <section class="section wrap" id="versions" aria-labelledby="versions-title">
    <h2 id="versions-title">{esc(strings['versionsTitle'])}</h2>
    <table class="versions">
      <thead><tr><th scope="col">{esc(strings['colSystem'])}</th><th scope="col">{esc(strings['colVersion'])}</th><th scope="col">{esc(strings['colStatus'])}</th><th scope="col" class="needs">{esc(strings['colNeeds'])}</th></tr></thead>
      <tbody>{versions_html(strings, releases)}</tbody>
    </table>
    <h3 class="sub" id="releases-title">{esc(strings['releasesTitle'])}</h3>
    {feature_sprite_html(releases)}
    <ol class="releases" aria-labelledby="releases-title">{releases_html(code, strings, releases)}</ol>
  </section>
  <section class="wrap">
    <div class="support">
      <div><h2>{esc(strings['donateTitle'])}</h2><p>{esc(strings['donateText'])}</p></div>
      <a class="btn btn-coffee" href="{DONATE}" rel="noopener">{esc(strings['donate'])}</a>
    </div>
  </section>
</main>
<footer class="foot wrap">{esc(strings['credit'])} {esc(strings['trademarks'])}</footer>
<script type="application/json" id="strings">{data_json}</script>
<script src="{root}themes.js"></script>
<script src="{root}app.js"></script>
</body>
</html>
"""


def themes_js():
    themes = []
    for theme_id in THEME_ORDER:
        meta = json.loads((THEMES / theme_id / "theme.json").read_text())
        layout = meta["layout"]
        themes.append({"id": theme_id, "kind": layout["kind"],
                       "aspect": layout.get("height", layout.get("size", 1)) / layout.get("size", 1),
                       "src": (THEMES / theme_id / "shader.glsl").read_text()})
    return "window.TUX_THEMES=" + json.dumps(themes) + ";\n"


def csp_hash(script):
    return "'sha256-" + base64.b64encode(hashlib.sha256(script.encode()).digest()).decode() + "'"


def parse_args(argv):
    parser = argparse.ArgumentParser(description="Builds the site into dist/.")
    parser.add_argument("--preview", metavar="UPCOMING.json",
                        help="build a local preview of planned releases into dist-preview/ instead")
    return parser.parse_args(argv)


def main(argv=()):
    options = parse_args(argv)
    languages = load_languages()
    releases = json.loads((SRC / "releases.json").read_text())
    dist = DIST
    if options.preview:
        apply_upcoming(options.preview, languages, releases)
        dist = DIST_PREVIEW
    check_releases(releases, languages)
    detect = detect_script(list(languages))
    policy = (f"default-src 'none'; script-src 'self' {csp_hash(MODE_SCRIPT)} {csp_hash(detect)}; "
              "style-src 'self'; img-src 'self'; base-uri 'none'; form-action 'none'")

    shutil.rmtree(dist, ignore_errors=True)
    dist.mkdir()
    for code, strings in languages.items():
        target = dist / path_for(code).strip("/") / "index.html"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(page(code, strings, languages, releases, detect, policy))
    shutil.copy(SRC / "style.css", dist)
    shutil.copy(SRC / "app.js", dist)
    shutil.copy(SRC / "favicon.svg", dist)
    (dist / "themes.js").write_text(themes_js())
    (dist / ".nojekyll").touch()    # served as they are, with no Jekyll build first
    (dist / "robots.txt").write_text("User-agent: *\nAllow: /\n")
    if options.preview:     # deploy.sh refuses a folder holding this file
        (dist / "LOCAL-PREVIEW").write_text("A local preview of planned releases. Never deploy it.\n")
        (dist / "robots.txt").write_text("User-agent: *\nDisallow: /\n")
    print(f"built {len(languages)} languages into {dist}"
          + (" (a local preview, never to be deployed)" if options.preview else ""))


if __name__ == "__main__":
    main(sys.argv[1:])
