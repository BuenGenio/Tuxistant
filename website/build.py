#!/usr/bin/env python3
"""Builds the static site into dist/: one page per file in src/i18n/.

Adding a language means adding src/i18n/<code>.json with the same keys as en.json.
SITE_URL adds canonical and hreflang URLs; deploy.sh sets it. Links are relative, so the same pages
work at a domain's root and under /Tuxistant/ on GitHub Pages. Pages can't set response headers,
so the security policy is a meta tag in each page.
"""
import base64
import hashlib
import html
import json
import os
import re
import shutil
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE / "src"
DIST = HERE / "dist"
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
    # Built, but not yet asked for by voice on a real machine, which is what earns "now".
    ("Google Calendar", "google-calendar", "soon"),
    ("The Kyiv Independent", "kyiv-independent", "soon"),
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
                                    "preview", "planned", "themes", "showTheme")}
    data["releases"] = releases
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
<main>
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
    </div>
  </section>
  <div class="wrap"><div class="ticker">{ticker_html(strings)}</div></div>
  <section class="section wrap" aria-labelledby="examples-title">
    <h2 id="examples-title">{esc(strings['examplesTitle'])}</h2>
    <div class="tabs" role="tablist" aria-labelledby="examples-title">{tabs}</div>
    {panels}
  </section>
  <section class="section wrap" id="versions" aria-labelledby="versions-title">
    <h2 id="versions-title">{esc(strings['versionsTitle'])}</h2>
    <table class="versions">
      <thead><tr><th scope="col">{esc(strings['colSystem'])}</th><th scope="col">{esc(strings['colVersion'])}</th><th scope="col">{esc(strings['colStatus'])}</th><th scope="col" class="needs">{esc(strings['colNeeds'])}</th></tr></thead>
      <tbody>{versions_html(strings, releases)}</tbody>
    </table>
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


def main():
    languages = load_languages()
    releases = json.loads((SRC / "releases.json").read_text())
    detect = detect_script(list(languages))
    policy = (f"default-src 'none'; script-src 'self' {csp_hash(MODE_SCRIPT)} {csp_hash(detect)}; "
              "style-src 'self'; img-src 'self'; base-uri 'none'; form-action 'none'")

    shutil.rmtree(DIST, ignore_errors=True)
    DIST.mkdir()
    for code, strings in languages.items():
        target = DIST / path_for(code).strip("/") / "index.html"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(page(code, strings, languages, releases, detect, policy))
    shutil.copy(SRC / "style.css", DIST)
    shutil.copy(SRC / "app.js", DIST)
    shutil.copy(SRC / "favicon.svg", DIST)
    (DIST / "themes.js").write_text(themes_js())
    (DIST / ".nojekyll").touch()    # served as they are, with no Jekyll build first
    (DIST / "robots.txt").write_text("User-agent: *\nAllow: /\n")
    print(f"built {len(languages)} languages into {DIST}")


if __name__ == "__main__":
    main()
