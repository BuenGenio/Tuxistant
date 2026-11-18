"""The skills portal: browse, search and a page per shared skill, built from what the registry
publishes (docs/REGISTRY-FORMAT.md): index.json and revocations.json with their signatures, and
recipes/<sha256>.yaml.

Everything in it is a stranger's text, so the portal takes only what Tux would, by Tux's own code:
tuxistant/registry.py checks both files' signatures and every field, finds the revocations in
effect, and checks each version that isn't revoked as Tux does before installing it. A version
Tux's loader refuses keeps its page, which says so; anything else that fails stops the build. Every
string is escaped into the page here, and no script builds markup from it. Logos aren't shown, as
Tux shows none yet.
"""
import difflib
import hashlib
import html
import sys
import tempfile
import time
import unicodedata
import urllib.parse
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
from tuxistant import apps, desktop, recipes, registry  # noqa: E402

# The registry repo's name waits for the owner (REGISTRY.md, "Not decided yet"). This is the
# suggested one, and the report form's field ids are this page's guess until the form exists.
REGISTRY_REPO = "BuenGenio/tuxistant-skills"
REPORT_TEMPLATE = "report.yml"
SAMPLE_SIGNERS = "registry_allowed_signers"     # the sample's own throwaway registry key
HIDDEN = registry.HIDDEN
KIND_WORDS = {"suspend": "Suspended", "ban": "Banned"}
RISK_WHY = {
    "low": "It reaches no network: it can only open something on your computer, change a "
           "desktop setting or say a sentence.",
    "medium": "It sends requests over the network, to {hosts} and nowhere else.",
}
DESKTOP_DOES = {
    "notify": "Shows a notification with a line written in the recipe, under a title that "
              "names it.",
    "do_not_disturb": "Turns Do Not Disturb {turn}.",
    "night_light": "Turns Night Light {turn}.",
    "dark_style": "Turns dark style {turn}.",
    "lock_screen": "Locks the screen.",
}

esc = html.escape


class Refused(ValueError):
    """The registry's files aren't what Tux would take, so no page is built from them."""


# Reading the registry, as Tux does

def is_sample(folder: Path) -> bool:
    """The made-up registry this site uses until the real one exists."""
    return (folder / "SAMPLE").is_file()


def signers_for(folder: Path) -> Path:
    """The registry key the index must be signed with: the one Tux ships, or for the sample its
    own throwaway key, which Tux never ships."""
    return folder / SAMPLE_SIGNERS if is_sample(folder) else registry.SHIPPED_SIGNERS


def _bytes(folder: Path, path: str, largest: int) -> bytes:
    file = folder / path
    if file.is_symlink() or not file.is_file():
        raise Refused(f"{path} is missing")
    with open(file, "rb") as handle:
        data = handle.read(largest + 1)
    if len(data) > largest:
        raise Refused(f"{path} is bigger than Tux takes")
    return data


def _reader(folder: Path):
    """The fetch Tux's Registry is handed: files from the folder, never the network."""
    def fetch(path: str, largest: int) -> bytes:
        return _bytes(folder, path, largest)
    return fetch


def load(folder: Path, signers: Path | None = None, clock=time.time) -> dict:
    """The registry as the portal shows it: categories, and every skill with at least one version
    that isn't revoked, newest update first. A skill whose current version Tux's loader refuses
    keeps its page, for whoever follows a link or a report to it, but has no card in Browse."""
    with tempfile.TemporaryDirectory(prefix="tux-portal-") as scratch:
        scratch = Path(scratch)
        tux = registry.Registry(scratch / "state", scratch / "skills", fetch=_reader(folder),
                                signers=signers or signers_for(folder), clock=clock)
        try:
            index = tux.accept_index(
                _bytes(folder, "index.json", registry.MAX_INDEX_BYTES),
                _bytes(folder, "index.json.sig", registry.MAX_SIG_BYTES))
            tux.accept_revocations(
                _bytes(folder, "revocations.json", registry.MAX_REVOCATIONS_BYTES),
                _bytes(folder, "revocations.json.sig", registry.MAX_SIG_BYTES))
            shown = [skill for skill in (_skill(tux, folder, index, skill, scratch)
                                         for skill in index["skills"]) if skill]
        except registry.Refused as e:
            raise Refused(str(e)) from None
    shown.sort(key=lambda skill: (skill["current"]["date"], skill["id"]), reverse=True)
    return {"categories": index["categories"], "skills": shown, "sample": is_sample(folder)}


def _skill(tux, folder: Path, index: dict, skill: dict, scratch: Path) -> dict | None:
    """A skill with each version as Tux sees it, or None when every version is revoked: hidden,
    as in Browse."""
    keys = {key["fingerprint"]: key["key"] for key in index["authors"][skill["author"]]["keys"]}
    versions = [{**version, "revoked": tux.covering(skill["id"], version["version"],
                                                    version["key"])}
                for version in skill["versions"]]
    if all(version["revoked"] for version in versions):
        return None
    for version in versions:
        version.update(_recipe(folder, skill, version, scratch))
        version["public_key"] = keys[version["key"]]
        # Only what Tux would install: the check Browse runs, signature and all.
        version["installable"] = bool(version["loaded"] and not version["revoked"])
        if version["installable"]:
            tux.check(skill["id"], version["version"])
    current = next(version for version in reversed(versions) if not version["revoked"])
    return {**skill, "versions": versions, "current": current,
            "file_name": skill["id"].rsplit(".", 1)[1] + ".yaml"}


def _recipe(folder: Path, skill: dict, version: dict, scratch: Path) -> dict:
    """The version's file, checked against the index, and what Tux's loader makes of it."""
    where = f"{skill['id']} {version['version']}"
    data = _bytes(folder, f"recipes/{version['sha256']}.yaml", registry.MAX_RECIPE_BYTES)
    if len(data) != version["size"] or hashlib.sha256(data).hexdigest() != version["sha256"]:
        raise Refused(f"the file for {where} isn't the one the index names")
    copy = scratch / f"{skill['id']}.yaml"
    copy.write_bytes(data)
    text = data.decode("utf-8", errors="replace")
    info = {"text": text, "bytes": data, "loaded": None, "problem": "", "raw": None}
    try:
        loaded = recipes.load(copy)
    except ValueError as e:
        info["problem"] = str(e)
        return info
    if (loaded["id"] != skill["id"] or loaded["version"] != version["version"]
            or loaded["permissions"] != version["permissions"]):
        raise Refused(f"{where} isn't what Tux's loader reads from its file")
    info["loaded"] = loaded
    info["raw"] = yaml.safe_load(text)      # the loader has already refused aliases and the rest
    return info


# Words

def hosts_of(permissions) -> list:
    return list(permissions.get("network", []))


def plain_list(items) -> str:
    items = list(items)
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def reach_words(permissions) -> str:
    hosts = hosts_of(permissions)
    if not hosts:
        return "Reaches no network."
    return f"Reaches {plain_list(hosts)} over the network, and nothing else."


def risk_why(risk, permissions) -> str:
    return RISK_WHY[risk].format(hosts=plain_list(hosts_of(permissions) or ["nothing"]))


def param_words(name, schema, required, raw) -> str:
    if "enum" in schema:
        kind = "one of " + ", ".join(map(str, schema["enum"]))
    elif schema.get("type") == "integer":
        kind = f"a whole number from {schema.get('minimum')} to {schema.get('maximum')}"
    else:
        kind = f"words, up to {schema.get('maxLength')} characters"
    extra = []
    if name in required:
        extra.append("needed")
    if isinstance(raw, dict) and "default" in raw:
        extra.append(f"if left out, {raw['default']}")
    return "; ".join([kind, *extra])


def settings_title(stem: str) -> str:
    for panel in apps.PANELS:
        if panel.desktop.removeprefix("gnome-").removesuffix("-panel.desktop") == stem:
            return panel.title
    return stem


def desktop_words(plan) -> tuple:
    """A desktop action, and whether Tux asks first, by the entry's own card rule in
    tuxistant/desktop.py, for each value the model can give it."""
    entry = desktop.DESKTOP.get(plan.entry)
    turn = dict(plan.settings).get("turn")
    turns = (True, False) if isinstance(turn, str) else (turn,)     # a choice, or fixed
    word = "on or off, as the model chooses" if len(turns) == 2 else "on" if turn else "off"
    does = DESKTOP_DOES.get(plan.entry, f"Does Tux's desktop action {plan.entry}.")
    cards = [entry.card({"turn": value}, plan.skill) is not None for value in turns] if entry \
        else []
    if cards and all(cards):
        card = "Tux shows you a card first, every time."
    elif cards == [True, False]:
        card = "Tux shows you a card first when it turns it on."
    else:
        card = "No card."
    return does.format(turn=word), card


def effect_words(plan, raw) -> tuple:
    """What the action does, and whether Tux asks first, as SKILLS.md "What needs a card" says."""
    if plan.effect == "open" and plan.page:
        return f"Opens the {settings_title(plan.page)} page of Settings.", "No card."
    if plan.effect == "open":
        return (f"Opens the app {plan.app}.", "No card for an app installed as a package. One "
                "added in your home folder shows a card with the command it runs.")
    if plan.effect == "desktop":
        return desktop_words(plan)
    if plan.effect == "say":
        return "Says a sentence written in the recipe.", "No card."
    method = plan.effect.upper()
    address = raw.get(plan.effect, "")
    does = f"Sends {method} {address} to {plan.host}."
    if plan.effect == "post":
        return does, "Tux shows you a card with the exact request every time."
    if plan.free_text:
        return does, ("Tux shows you a card with the exact request first, as words you say go "
                      "into it.")
    return does, ("No card in a fresh conversation, as only numbers or choices from its list go "
                  "into it. A card if the host is on your local network or the conversation has "
                  "read something private.")


def changes(old, new) -> list:
    """What a reviewer reads first: new hosts and actions, and anything the model sees."""
    if not (old["loaded"] and new["loaded"]):
        return ["Tux's loader refuses one of these versions, so only the text is compared."]
    before, after = old["loaded"], new["loaded"]
    words = []
    old_hosts, new_hosts = hosts_of(before["permissions"]), hosts_of(after["permissions"])
    words += [f"New host: {host}" for host in new_hosts if host not in old_hosts]
    words += [f"No longer reaches {host}" for host in old_hosts if host not in new_hosts]
    words += [f"New action: {name}" for name in after["actions"] if name not in before["actions"]]
    words += [f"Action removed: {name}" for name in before["actions"]
              if name not in after["actions"]]
    for name, action in after["actions"].items():
        was = before["actions"].get(name)
        if not was:
            continue
        if was["description"] != action["description"]:
            words.append(f"The model sees a new description for {name}")
        if (was["params"], was["required"]) != (action["params"], action["required"]):
            words.append(f"The model can send different values to {name}")
    return words or ["Nothing the model sees and no host changed."]


# Markup. Every value from the registry goes through esc() or code() on its way in.

def code(text: str) -> str:
    """Text for a <pre>, with hidden characters shown as their code: a right-to-left override in a
    comment could otherwise make the file read differently from what it is."""
    out = []
    for char in text:
        if char not in "\n\t" and unicodedata.category(char) in HIDDEN:
            out.append(f'<span class="hidden-char" title="A hidden character">'
                       f'U+{ord(char):04X}</span>')
        else:
            out.append(esc(char))
    return "".join(out)


def diff_html(old, new) -> str:
    lines = difflib.unified_diff(old["text"].splitlines(), new["text"].splitlines(),
                                 lineterm="", n=2)
    out = []
    for line in lines:
        if line.startswith(("---", "+++")):
            continue
        kind = {"+": "add", "-": "del", "@": "hunk"}.get(line[:1], "same")
        out.append(f'<span class="{kind}">{code(line)}</span>')
    return '<pre class="code diff">' + "\n".join(out) + "</pre>" if out else ""


def risk_badge(risk) -> str:
    return f'<span class="risk risk-{esc(risk)}">{esc(risk.capitalize())} risk</span>'


def tag_chips(tags, base) -> str:
    return "".join(f'<a class="chip" href="{base}?tag={esc(tag)}">{esc(tag)}</a>' for tag in tags)


def report_url(skill, version) -> str:
    query = urllib.parse.urlencode({
        "template": REPORT_TEMPLATE, "title": f"Report: {skill['id']} {version['version']}",
        "skill": skill["id"], "version": version["version"], "sha256": version["sha256"]})
    return f"https://github.com/{REGISTRY_REPO}/issues/new?{query}"


SAMPLE_NOTE = ('<p class="sample" role="note"><strong>Sample.</strong> The registry isn\'t open '
               "yet. These made-up skills show how its pages will look. There is nothing to "
               "install or report.</p>")
LEDE = {
    True: "Recipe skills people will share for Tuxistant. Each will be signed by its publisher and "
          "reviewed before it's merged, and Tux will check it again before installing.",
    False: "Recipe skills people share for Tuxistant. Each is signed by its publisher and reviewed "
           "before it's merged, and Tux checks it again before installing.",
}
# The calls to action: Tuxistant itself, as skills install from its Settings.
ASK = {
    True: "Once the registry opens, skills like these install from Tuxistant's Settings. Want it "
          "on your desktop?",
    False: "Skills install from Tuxistant's Settings. Want it on your desktop?",
}
REFUSED = "Tux's loader refuses this version, so it can't be installed: "


def card_html(skill, categories) -> str:
    current = skill["current"]
    in_categories = [c["id"] for c in categories if set(c["tags"]) & set(skill["tags"])]
    search = " ".join([skill["name"], skill["summary"], skill["id"], skill["author"],
                       *skill["tags"]]).lower()
    return (f'<li class="card" data-tags="{esc(" ".join(skill["tags"]))}" '
            f'data-categories="{esc(" ".join(in_categories))}" data-search="{esc(search)}">'
            f'<h3><a href="{esc(skill["id"])}/">{esc(skill["name"])}</a></h3>'
            f'<p>{esc(skill["summary"])}</p>'
            f'<div class="meta-line">{risk_badge(current["risk"])}'
            f'<span>by {esc(skill["author"])}</span><span>{esc(current["version"])}, '
            f'{esc(current["date"][:10])}</span></div>'
            f'<div class="chips">{tag_chips(skill["tags"], "")}</div></li>')


def cta_html(site, sample) -> str:
    return (f'<div class="acc-cta portal-cta"><p>{esc(ASK[sample])}</p>'
            f'<div class="acc-actions">{site.download}</div></div>')


def browse_html(shown, site) -> str:
    categories = shown["categories"]
    cats = "".join(
        f'<div class="cat"><a class="cat-name" href="?category={esc(c["id"])}" '
        f'data-filter="category:{esc(c["id"])}">{esc(c["name"])}</a>'
        + "".join(f'<a class="chip" href="?tag={esc(t)}" data-filter="tag:{esc(t)}">{esc(t)}</a>'
                  for t in c["tags"]) + "</div>"
        for c in categories)
    listed = [skill for skill in shown["skills"] if skill["current"]["installable"]]
    cards = "".join(card_html(skill, categories) for skill in listed)
    count = len(listed)
    sample = shown["sample"]
    return f"""<section class="portal wrap" aria-labelledby="skills-title">
  <div class="portal-head">
    <h1 id="skills-title">Skills</h1>
    <p class="lede">{LEDE[sample]}</p>
    {SAMPLE_NOTE if sample else ""}
  </div>
  <div class="finder" id="finder" hidden><label class="visually-hidden" for="skill-search">Search skills</label><input id="skill-search" type="search" placeholder="Search skills" autocomplete="off" spellcheck="false"></div>
  <nav class="cats" aria-label="Categories and tags"><a class="cat-name" href="./" data-filter="" aria-current="true">All</a>{cats}</nav>
  <h2 class="count" id="skill-count" aria-live="polite">{count} skill{"" if count == 1 else "s"}</h2>
  <ul class="cards" id="skill-list">{cards}</ul>
  <p class="none" id="skill-none"{"" if not count else " hidden"}>No skills match. Try fewer words, or All.</p>
  {cta_html(site, sample)}
</section>"""


def action_html(name, action, raw_action) -> str:
    does, card = effect_words(action["plan"], raw_action or {})
    raw_params = (raw_action or {}).get("params") or {}
    params = "".join(
        f'<li><code>{esc(p)}</code>: {esc(schema.get("description", ""))} <span class="muted">'
        f'({esc(param_words(p, schema, action["required"], raw_params.get(p)))})</span></li>'
        for p, schema in action["params"].items())
    say = (raw_action or {}).get("say")
    said = (f'<p>Then says: <q>{esc(str(say))}</q></p>'
            if say and action["plan"].effect in ("get", "post") else "")
    return (f'<div class="action"><h3>{esc(name)}</h3>'
            f'<div class="model"><p class="label">What the model sees</p>'
            f'<p class="model-text">{esc(action["description"])}</p>'
            + (f'<ul class="params">{params}</ul>' if params else
               '<p class="muted">It takes no values.</p>')
            + f'</div><p>{esc(does)} {esc(card)}</p>{said}</div>')


def by_hand_html(skill, current) -> str:
    name = skill["file_name"]
    signers = f"{name.removesuffix('.yaml')}.signers"
    principal = f"io.github.{skill['author']}"
    commands = (f"echo '{current['sha256']}  {name}' | sha256sum -c\n"
                f"echo '{principal} {current['public_key']}' > {signers}\n"
                f"ssh-keygen -Y verify -f {signers} -I {principal} -n {registry.RECIPE_NS} "
                f"-s {name}.sig < {name}\n"
                f"mv {name} ~/.local/share/tuxistant/skills/")
    file = f'../recipes/{esc(current["sha256"])}.yaml'
    return (f'<li>By hand: download <a href="{file}" download="{esc(name)}">{esc(name)}</a> and '
            f'<a href="{file}.sig" download="{esc(name)}.sig">its signature</a>, check them, and '
            f'put the recipe in your skills folder:\n<pre class="code">{esc(commands)}</pre>\n'
            "      A recipe added by hand isn't switched off if the registry later revokes it.</li>")


def install_html(skill, current, sample, site) -> str:
    """Install steps. The sample's files are made up, so its pages offer neither them nor the
    commands, and nothing is offered for a file Tux's loader refuses."""
    if not current["installable"]:
        return ""
    by_hand = ('<li><span class="tag tag-soon">Soon</span> By hand: the recipe, its signature and '
               'the commands that check them.</li>' if sample else by_hand_html(skill, current))
    card = ("Install will show you a card with the hosts it reaches and its risk first, and Tux "
            "will check the file's hash and signature itself." if sample else
            "Install shows you a card with the hosts it reaches and its risk first, and Tux checks "
            "the file's hash and signature itself.")
    return f"""{site.acc_open("install", "Install")}
    <ol class="steps">
      <li><span class="tag tag-soon">Soon</span> In Tuxistant's Settings, open Skills, then Browse, and search for {esc(skill["name"])}. {card}</li>
      {by_hand}
    </ol>
  {site.acc_close(ASK[sample], site.download)}"""


def version_html(skill, versions, position) -> str:
    version = versions[position]
    revoked = version["revoked"]
    note = ""
    if revoked:
        report = (f' <a href="{esc(revoked["report"])}" rel="nofollow noopener">The report</a>.'
                  if revoked["report"] else "")
        note = (f'<p class="revoked"><strong>{KIND_WORDS[revoked["kind"]]}:</strong> '
                f'{esc(revoked["reason"])}{report}</p>')
    problem = (f'<p class="revoked">Tux\'s loader refuses this version: '
               f'{esc(version["problem"])}</p>' if version["problem"] else "")
    if position == 0:
        changed = '<p class="muted">The first version.</p>'
    else:
        before = versions[position - 1]
        items = "".join(f"<li>{esc(line)}</li>" for line in changes(before, version))
        changed = (f'<details><summary>What changed from {esc(before["version"])}</summary>'
                   f'<ul class="changes">{items}</ul>{diff_html(before, version)}</details>')
    # Risk and hosts only for a file Tux loads, as nothing else backs up what the index says.
    loaded = version["loaded"]
    return (f'<li class="version"><div class="version-head"><strong>{esc(version["version"])}'
            f'</strong><span class="muted">{esc(version["date"][:10])}</span>'
            f'{risk_badge(version["risk"]) if loaded else ""}</div>'
            f'<p class="file-meta">{esc(reach_words(version["permissions"])) + " " if loaded else ""}'
            f'Signed with {esc(version["key"])}. SHA-256 {esc(version["sha256"])}.</p>'
            f"{note}{problem}{changed}</li>")


def skill_html(skill, sample, site) -> str:
    """A skill's page: what it is at the top, then its sections, folded as the front page's are,
    with what it does open first."""
    current = skill["current"]
    loaded = current["loaded"]
    raw_actions = (current["raw"] or {}).get("actions") or {}
    if loaded:
        actions = (f'<p class="muted">{esc(reach_words(current["permissions"]))} Each action below '
                   "is one the model can choose, by the words it sees.</p>"
                   + "".join(action_html(name, action, raw_actions.get(name))
                             for name, action in loaded["actions"].items()))
    else:
        # The index's hosts and risk aren't shown: only a file Tux loads backs them up.
        actions = '<p class="muted">Not shown, as Tux wouldn\'t load the file.</p>'
    why = (f'{risk_badge(current["risk"])} {esc(risk_why(current["risk"], current["permissions"]))}'
           if loaded else f'<span class="revoked">{esc(REFUSED + current["problem"])}</span>')
    # The sample has nothing to install or report, so its pages have neither button.
    buttons = [] if sample else (
        (['<a class="btn btn-cta" href="#install">Install</a>'] if current["installable"] else [])
        + [f'<a class="btn" href="{esc(report_url(skill, current))}" '
           'rel="nofollow noopener">Report this skill</a>'])
    links = "".join(f'<a href="{esc(link["url"])}" rel="nofollow noopener ugc">'
                    f'{esc(link["title"])}</a>' for link in skill["links"])
    history = "".join(version_html(skill, skill["versions"], i)
                      for i in reversed(range(len(skill["versions"]))))
    return f"""<article class="portal" aria-labelledby="skill-title">
  <div class="portal-head wrap">
    <nav class="crumbs" aria-label="Breadcrumb"><a href="../">Skills</a></nav>
    <h1 id="skill-title" class="fit">{esc(skill["name"])}</h1>
    <p class="lede">{esc(skill["summary"])}</p>
    {SAMPLE_NOTE if sample else ""}
    <p class="why">{why}</p>
    <dl class="facts">
      <div><dt>Publisher</dt><dd><a href="https://github.com/{esc(skill["author"])}" rel="nofollow noopener">{esc(skill["author"])}</a></dd></div>
      <div><dt>Version</dt><dd>{esc(current["version"])}, {esc(current["date"][:10])}</dd></div>
      <div><dt>Licence</dt><dd>{esc(skill["licence"])}</dd></div>
      <div><dt>Id</dt><dd><code>{esc(skill["id"])}</code></dd></div>
      <div><dt>Tags</dt><dd class="chips">{tag_chips(skill["tags"], "../")}</dd></div>
      {f'<div><dt>Links</dt><dd class="links">{links}</dd></div>' if links else ""}
    </dl>
    {f'<div class="buttons">{"".join(buttons)}</div>' if buttons else ""}
  </div>
  {site.acc_open("does", "What it does")}
    {actions}
  {site.acc_end()}
  {install_html(skill, current, sample, site)}
  {site.acc_open("file", "The whole recipe")}
    <p class="file-meta">{esc(skill["file_name"])}, version {esc(current["version"])}, {current["size"]} bytes. SHA-256 {esc(current["sha256"])}.</p>
    <pre class="code">{code(current["text"])}</pre>
  {site.acc_end()}
  {site.acc_open("versions", "Versions")}
    <ol class="history" reversed>{history}</ol>
  {site.acc_end()}
</article>"""


def build(dist: Path, folder: Path, site, signers: Path | None = None) -> int:
    """Writes skills/, skills/<id>/ and, for a real registry, skills/recipes/. site holds the
    site's own pieces: shell(title, description, depth, body, sample) wraps a body in the site's
    page, acc_open, acc_close and acc_end make its folding sections, and download is its download
    button. The index must be signed by the registry key in signers, which is the one Tux ships
    unless told otherwise, or the sample's own. Returns how many skill pages it wrote."""
    shown = load(folder, signers)
    out = dist / "skills"
    out.mkdir(parents=True)
    sample = shown["sample"]
    (out / "index.html").write_text(site.shell(
        "Skills", LEDE[sample].split(". ")[0] + ".", 1, browse_html(shown, site), sample))
    for skill in shown["skills"]:
        page = out / skill["id"] / "index.html"
        page.parent.mkdir()
        page.write_text(site.shell(skill["name"], skill["summary"], 2,
                                   skill_html(skill, sample, site), sample))
        # Only versions Tux would install, and never the sample's.
        served = [] if sample else [v for v in skill["versions"] if v["installable"]]
        for version in served:
            target = out / "recipes" / f"{version['sha256']}.yaml"
            target.parent.mkdir(exist_ok=True)
            target.write_bytes(version["bytes"])
            # The signature the index carries, which is the one Tux checks.
            target.with_name(target.name + ".sig").write_text(version["sig"])
    return len(shown["skills"])
