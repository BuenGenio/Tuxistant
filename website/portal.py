"""The skills portal: browse, search and a page per shared skill, built from what the registry
publishes (docs/REGISTRY-FORMAT.md): index.json, revocations.json and recipes/<sha256>.yaml.

Everything in it is a stranger's text. The index is checked against the format and refused whole
if anything doesn't fit, each recipe must have the size and hash the index gives, and every string
is escaped into the page here. No script builds markup from it. Recipes go through Tux's own
loader, so what a page says an action does is what Tux would do with that file.
"""
import difflib
import hashlib
import html
import json
import re
import sys
import tempfile
import unicodedata
import urllib.parse
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
from tuxistant import apps, recipes  # noqa: E402
from tuxistant.skills import risk_of  # noqa: E402

# The registry repo's name waits for the owner (REGISTRY.md, "Not decided yet"). This is the
# suggested one, and the report form's field ids are this page's guess until the form exists.
REGISTRY_REPO = "BuenGenio/tuxistant-skills"
REPORT_TEMPLATE = "report.yml"
MAX_INDEX = 4 << 20
MAX_REVOCATIONS = 1 << 20
MAX_RECIPE = 65536
HIDDEN = ("Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp")
KEY_TYPES = ("ssh-ed25519", "sk-ssh-ed25519@openssh.com", "ecdsa-sha2-nistp256",
             "ecdsa-sha2-nistp384", "ecdsa-sha2-nistp521", "sk-ecdsa-sha2-nistp256@openssh.com",
             "ssh-rsa")
LOGIN = r"[a-z0-9]+(?:-[a-z0-9]+)*"
SKILL_NAME = r"[a-z0-9][a-z0-9-]{0,39}"
SKILL_ID = re.compile(rf"io\.github\.({LOGIN})\.({SKILL_NAME})")
TAG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
DAY = re.compile(r"\d{4}-\d{2}-\d{2}")
VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9.+-]{0,39}")
SHA256 = re.compile(r"[0-9a-f]{64}")
COMMIT = re.compile(r"[0-9a-f]{40}")
FINGERPRINT = re.compile(r"SHA256:[A-Za-z0-9+/]{43}")
KEY = re.compile(rf"(?:{'|'.join(map(re.escape, KEY_TYPES))}) [A-Za-z0-9+/]+={{0,3}}")
REPORT = re.compile(r"https://github\.com/[A-Za-z0-9-]+/[A-Za-z0-9._-]+/issues/[0-9]+")
LICENCES = ("MIT", "CC0-1.0")
INDEX_FIELDS = {"format", "seq", "date", "commit", "moderation_keys", "categories", "authors",
                "skills", "revocations", "lifted"}
SKILL_FIELDS = {"id", "author", "name", "summary", "tags", "licence", "links", "versions"}
VERSION_FIELDS = {"version", "date", "sha256", "size", "key", "sig", "permissions", "risk"}
REVOCATION_FIELDS = {"id", "kind", "scope", "reason", "date", "actor", "report"}
LIFT_FIELDS = {"id", "reason", "date", "actor", "report"}
KIND_WORDS = {"suspend": "Suspended", "ban": "Banned"}
RISK_WHY = {
    "low": "It reaches no network: it can only open something on your computer or say a sentence.",
    "medium": "It sends requests over the network, to {hosts} and nowhere else.",
}

esc = html.escape


class Refused(ValueError):
    """The registry's files don't fit the format, so no page is built from them."""


# Checking the index

def _need(ok, what):
    if not ok:
        raise Refused(what)


def _fields(value, fields, where):
    _need(isinstance(value, dict) and set(value) == fields, f"{where} doesn't have exactly the "
          f"fields {', '.join(sorted(fields))}")


def _match(pattern, value, where, longest=None):
    _need(isinstance(value, str) and pattern.fullmatch(value)
          and (longest is None or len(value) <= longest), f"{where} isn't in the right form")
    return value


def _text(value, longest, where):
    """text(n) from the format: one line, nothing hidden, no < or >, no space at either end."""
    _need(isinstance(value, str) and 0 < len(value) <= longest and value == value.strip()
          and not any(unicodedata.category(c) in HIDDEN or c in "<>" for c in value),
          f"{where} isn't one line of up to {longest} plain characters")
    return value


def _int(value, where, low=1, high=None):
    _need(type(value) is int and value >= low and (high is None or value <= high),
          f"{where} isn't a whole number in range")
    return value


def _login(value, where):
    return _match(re.compile(LOGIN), value, where, 39)


def _link(link, where):
    _fields(link, {"title", "url"}, where)
    _text(link["title"], 30, f"{where} title")
    url = link["url"]
    _need(isinstance(url, str) and url.startswith("https://") and len(url) <= 200
          and not any(c.isspace() or unicodedata.category(c) in HIDDEN for c in url)
          and "@" not in urllib.parse.urlsplit(url).netloc
          and not any(c in url for c in "<>\"'`\\"), f"{where} isn't a plain https address")


def _categories(categories):
    _need(isinstance(categories, list) and 1 <= len(categories) <= 30, "categories isn't a list")
    seen_ids, seen_tags = set(), set()
    for category in categories:
        _fields(category, {"id", "name", "tags"}, "a category")
        _match(TAG, category["id"], "a category id", 24)
        _text(category["name"], 30, "a category name")
        tags = category["tags"]
        _need(isinstance(tags, list) and 1 <= len(tags) <= 30, "a category's tags isn't a list")
        for tag in tags:
            _need(isinstance(tag, str) and 2 <= len(tag) <= 24 and TAG.fullmatch(tag),
                  "a tag isn't in the right form")
            _need(tag not in seen_tags, f"the tag {tag} is in two categories")
            seen_tags.add(tag)
        _need(category["id"] not in seen_ids, "two categories share an id")
        seen_ids.add(category["id"])
    return seen_tags


def _authors(authors):
    _need(isinstance(authors, dict), "authors isn't an object")
    keys = {}
    for login, author in authors.items():
        _login(login, "an author's login")
        _fields(author, {"status", "keys"}, f"author {login}")
        _need(author["status"] in ("active", "suspended", "banned"), f"{login} has no status")
        _need(isinstance(author["keys"], list) and author["keys"], f"{login} has no keys")
        for key in author["keys"]:
            _fields(key, {"key", "fingerprint", "added", "removed"}, f"a key of {login}")
            _match(KEY, key["key"], f"a key of {login}", 2000)
            _match(FINGERPRINT, key["fingerprint"], f"a fingerprint of {login}")
            _match(DAY, key["added"], f"a key date of {login}")
            _need(key["removed"] is None or DAY.fullmatch(str(key["removed"])),
                  f"a key date of {login} isn't a day")
            keys[(login, key["fingerprint"])] = key["key"]
    return keys


def _version(version, where):
    _fields(version, VERSION_FIELDS, where)
    _match(VERSION, version["version"], f"{where} version")
    _match(TIMESTAMP, version["date"], f"{where} date")
    _match(SHA256, version["sha256"], f"{where} sha256")
    _int(version["size"], f"{where} size", 1, MAX_RECIPE)
    _match(FINGERPRINT, version["key"], f"{where} key")
    _need(isinstance(version["sig"], str) and len(version["sig"]) <= 4096
          and version["sig"].startswith("-----BEGIN SSH SIGNATURE-----\n")
          and version["sig"].endswith("-----END SSH SIGNATURE-----\n"), f"{where} sig isn't one")
    permissions = version["permissions"]
    _need(isinstance(permissions, dict) and set(permissions) <= {"network"}
          and all(isinstance(h, str) for h in permissions.get("network", []))
          and (not permissions or 1 <= len(permissions["network"]) <= 4),
          f"{where} permissions aren't a recipe's")
    _need(version["risk"] == risk_of(permissions), f"{where} risk isn't what Tux works out")


def _skill(skill, tags, keys):
    _fields(skill, SKILL_FIELDS, "a skill")
    match = SKILL_ID.fullmatch(skill["id"]) if isinstance(skill["id"], str) else None
    _need(match and len(skill["id"]) <= 100, "a skill id isn't io.github.<login>.<name>")
    where = skill["id"]
    _need(skill["author"] == match[1], f"{where} has an author that isn't in its id")
    _text(skill["name"], 100, f"{where} name")
    _text(skill["summary"], 140, f"{where} summary")
    _need(isinstance(skill["tags"], list) and 1 <= len(skill["tags"]) <= 4
          and len(set(skill["tags"])) == len(skill["tags"])
          and all(isinstance(t, str) and t in tags for t in skill["tags"]),
          f"{where} has tags that aren't in the list")
    _need(skill["licence"] in LICENCES, f"{where} isn't MIT or CC0-1.0")
    _need(isinstance(skill["links"], list) and len(skill["links"]) <= 3, f"{where} links")
    for link in skill["links"]:
        _link(link, f"a link of {where}")
    _need(isinstance(skill["versions"], list) and skill["versions"], f"{where} has no versions")
    seen = set()
    for version in skill["versions"]:
        _version(version, where)
        _need(version["version"] not in seen, f"{where} lists a version twice")
        _need((skill["author"], version["key"]) in keys,
              f"{where} {version['version']} is signed with a key its author doesn't have")
        seen.add(version["version"])


def _scope(scope, kind, where):
    _need(isinstance(scope, dict), f"{where} scope isn't an object")
    if set(scope) == {"skill"} or set(scope) == {"skill", "versions"}:
        _need(kind == "suspend", f"{where} bans a skill, and a ban is for an author or a key")
        _need(isinstance(scope["skill"], str) and SKILL_ID.fullmatch(scope["skill"]),
              f"{where} names a skill that isn't one")
        if "versions" in scope:
            versions = scope["versions"]
            _need(isinstance(versions, list) and 1 <= len(versions) <= 50
                  and all(isinstance(v, str) and VERSION.fullmatch(v) for v in versions),
                  f"{where} versions")
    elif set(scope) == {"author"}:
        _login(scope["author"], f"{where} author")
    elif set(scope) == {"key"}:
        _match(FINGERPRINT, scope["key"], f"{where} key")
    else:
        raise Refused(f"{where} scope isn't one of the three forms")


def _entry(entry, fields):
    where = "a revocation" if fields is REVOCATION_FIELDS else "a lift"
    _fields(entry, fields, where)
    _int(entry["id"], f"{where} id")
    _text(entry["reason"], 300, f"{where} reason")
    _match(TIMESTAMP, entry["date"], f"{where} date")
    _login(entry["actor"], f"{where} actor")
    _need(entry["report"] is None or (isinstance(entry["report"], str) and len(entry["report"])
                                      <= 200 and REPORT.fullmatch(entry["report"])),
          f"{where} report isn't an issue address")
    if fields is REVOCATION_FIELDS:
        _need(entry["kind"] in KIND_WORDS, f"{where} kind")
        _scope(entry["scope"], entry["kind"], where)
    return entry


def check_index(index):
    _fields(index, INDEX_FIELDS, "index.json")
    _need(index["format"] == 1, "index.json is a newer format than this site knows")
    _int(index["seq"], "seq")
    _match(TIMESTAMP, index["date"], "the index date")
    _match(COMMIT, index["commit"], "the index commit")
    _need(isinstance(index["moderation_keys"], list) and 1 <= len(index["moderation_keys"]) <= 3
          and all(isinstance(k, str) and k.startswith("ssh-ed25519 ") and KEY.fullmatch(k)
                  for k in index["moderation_keys"]), "moderation_keys")
    tags = _categories(index["categories"])
    keys = _authors(index["authors"])
    _need(isinstance(index["skills"], list), "skills isn't a list")
    for skill in index["skills"]:
        _skill(skill, tags, keys)
        _need(skill["author"] in index["authors"], f"{skill['id']} has no author entry")
    ids = [skill["id"] for skill in index["skills"]]
    _need(len(set(ids)) == len(ids), "two skills share an id")
    for name, fields in (("revocations", REVOCATION_FIELDS), ("lifted", LIFT_FIELDS)):
        _need(isinstance(index[name], list), f"{name} isn't a list")
        for entry in index[name]:
            _entry(entry, fields)
    return keys


def _read_json(file: Path, largest: int):
    _need(file.is_file() and not file.is_symlink(), f"{file.name} is missing")
    data = file.read_bytes()
    _need(len(data) <= largest, f"{file.name} is too big")
    try:
        return json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise Refused(f"{file.name} isn't JSON") from None


def _revocations(folder: Path, index) -> dict:
    """In effect, by id: the index's entries, then any newer ones from revocations.json. As in
    Tux, the first copy of an id stands, and only the index can lift one."""
    entries = {}
    extra = []
    if (folder / "revocations.json").exists():
        published = _read_json(folder / "revocations.json", MAX_REVOCATIONS)
        _fields(published, {"format", "seq", "date", "revocations"}, "revocations.json")
        _need(published["format"] == 1, "revocations.json is a newer format")
        _need(isinstance(published["revocations"], list), "revocations.json revocations")
        extra = [_entry(entry, REVOCATION_FIELDS) for entry in published["revocations"]]
    for entry in index["revocations"] + extra:
        entries.setdefault(entry["id"], entry)
    lifted = {entry["id"] for entry in index["lifted"]}
    return {key: entry for key, entry in entries.items() if key not in lifted}


def covering(skill, version, revocations):
    """The revocation in effect for this version, if any."""
    for entry in revocations.values():
        scope = entry["scope"]
        if scope.get("skill") == skill["id"]:
            if version["version"] in scope.get("versions", [version["version"]]):
                return entry
        elif scope.get("author") == skill["author"] or scope.get("key") == version["key"]:
            return entry
    return None


# Reading the recipes

def _recipe(folder: Path, skill, version, scratch: Path):
    """The version's file, checked against the index, and what Tux's loader makes of it."""
    file = folder / "recipes" / f"{version['sha256']}.yaml"
    where = f"{skill['id']} {version['version']}"
    _need(file.is_file() and not file.is_symlink(), f"the file for {where} is missing")
    data = file.read_bytes()
    _need(len(data) == version["size"] and hashlib.sha256(data).hexdigest() == version["sha256"],
          f"the file for {where} isn't the one the index names")
    copy = scratch / f"{skill['id'].rsplit('.', 1)[1]}.yaml"
    copy.write_bytes(data)
    text = data.decode("utf-8", errors="replace")
    info = {"text": text, "bytes": data, "loaded": None, "problem": "", "raw": None}
    try:
        loaded = recipes.load(copy)
    except ValueError as e:
        info["problem"] = str(e)
        return info
    _need(loaded["id"] == skill["id"] and loaded["version"] == version["version"]
          and loaded["permissions"] == version["permissions"],
          f"{where} isn't what Tux's loader reads from its file")
    info["loaded"] = loaded
    info["raw"] = yaml.safe_load(text)      # the loader has already refused aliases and the rest
    return info


def load(folder: Path) -> dict:
    """The registry as the portal shows it: categories, and every skill with at least one version
    that isn't revoked, newest update first."""
    index = _read_json(folder / "index.json", MAX_INDEX)
    keys = check_index(index)
    revocations = _revocations(folder, index)
    shown = []
    with tempfile.TemporaryDirectory() as scratch:
        for skill in index["skills"]:
            versions = [{**version, "revoked": covering(skill, version, revocations)}
                        for version in skill["versions"]]
            if all(version["revoked"] for version in versions):
                continue        # suspended or banned: hidden, as in Browse
            for version in versions:
                version.update(_recipe(folder, skill, version, Path(scratch)))
                version["public_key"] = keys[(skill["author"], version["key"])]
            current = next(v for v in reversed(versions) if not v["revoked"])
            shown.append({**skill, "versions": versions, "current": current,
                          "file_name": skill["id"].rsplit(".", 1)[1] + ".yaml"})
    shown.sort(key=lambda skill: (skill["current"]["date"], skill["id"]), reverse=True)
    return {"categories": index["categories"], "skills": shown,
            "sample": (folder / "SAMPLE").is_file()}


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


def effect_words(plan, raw) -> tuple:
    """What the action does, and whether Tux asks first, as SKILLS.md "What needs a card" says."""
    if plan.effect == "open" and plan.page:
        return f"Opens the {settings_title(plan.page)} page of Settings.", "No card."
    if plan.effect == "open":
        return f"Opens the app {plan.app}.", "No card for an app in the app grid."
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
               "yet. These made-up skills show how its pages will look, and can't be installed."
               "</p>")


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


def browse_html(registry) -> str:
    categories = registry["categories"]
    cats = "".join(
        f'<div class="cat"><a class="cat-name" href="?category={esc(c["id"])}" '
        f'data-filter="category:{esc(c["id"])}">{esc(c["name"])}</a>'
        + "".join(f'<a class="chip" href="?tag={esc(t)}" data-filter="tag:{esc(t)}">{esc(t)}</a>'
                  for t in c["tags"]) + "</div>"
        for c in categories)
    cards = "".join(card_html(skill, categories) for skill in registry["skills"])
    count = len(registry["skills"])
    return f"""<section class="portal wrap" aria-labelledby="skills-title">
  <h1 id="skills-title">Skills</h1>
  <p class="lede">Recipe skills people share for Tuxistant. Each is reviewed and signed by its publisher, and Tux checks it again before it installs.</p>
  {SAMPLE_NOTE if registry["sample"] else ""}
  <div class="finder" id="finder" hidden><label class="visually-hidden" for="skill-search">Search skills</label><input id="skill-search" type="search" placeholder="Search skills" autocomplete="off" spellcheck="false"></div>
  <nav class="cats" aria-label="Categories and tags"><a class="cat-name" href="./" data-filter="" aria-current="true">All</a>{cats}</nav>
  <h2 class="count" id="skill-count" aria-live="polite">{count} skill{"" if count == 1 else "s"}</h2>
  <ul class="cards" id="skill-list">{cards}</ul>
  <p class="none" id="skill-none"{"" if not count else " hidden"}>No skills match. Try fewer words, or All.</p>
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


def install_html(skill, current) -> str:
    name = skill["file_name"]
    signers = f"{name.removesuffix('.yaml')}.signers"
    principal = f"io.github.{skill['author']}"
    commands = (f"echo '{current['sha256']}  {name}' | sha256sum -c\n"
                f"echo '{principal} {current['public_key']}' > {signers}\n"
                f"ssh-keygen -Y verify -f {signers} -I {principal} -n tuxistant-recipe "
                f"-s {name}.sig < {name}\n"
                f"mv {name} ~/.local/share/tuxistant/skills/")
    return f"""<section class="section" id="install" aria-labelledby="install-title">
    <h2 id="install-title">Install</h2>
    <ol class="steps">
      <li><span class="tag tag-soon">Soon</span> In Tuxistant's Settings, open Skills, then Browse, and search for {esc(skill["name"])}. Install shows you a card with the hosts it reaches and its risk first, and Tux checks the file's hash and signature itself.</li>
      <li>By hand: download <a href="../recipes/{esc(current["sha256"])}.yaml" download="{esc(name)}">{esc(name)}</a> and <a href="../recipes/{esc(current["sha256"])}.yaml.sig" download="{esc(name)}.sig">its signature</a>, check them, and put the recipe in your skills folder:
<pre class="code">{esc(commands)}</pre>
      A recipe added by hand isn't switched off if the registry later revokes it.</li>
    </ol>
  </section>"""


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
    return (f'<li class="version"><div class="version-head"><strong>{esc(version["version"])}'
            f'</strong><span class="muted">{esc(version["date"][:10])}</span>'
            f'{risk_badge(version["risk"])}</div>'
            f'<p class="file-meta">{esc(reach_words(version["permissions"]))} Signed with '
            f'{esc(version["key"])}. SHA-256 {esc(version["sha256"])}.</p>'
            f"{note}{problem}{changed}</li>")


def skill_html(skill, sample) -> str:
    current = skill["current"]
    loaded = current["loaded"]
    raw_actions = (current["raw"] or {}).get("actions") or {}
    if loaded:
        actions = "".join(action_html(name, action, raw_actions.get(name))
                          for name, action in loaded["actions"].items())
    else:
        actions = (f'<p class="revoked">Tux\'s loader refuses this version: '
                   f'{esc(current["problem"])}</p>')
    links = "".join(f'<a href="{esc(link["url"])}" rel="nofollow noopener ugc">'
                    f'{esc(link["title"])}</a>' for link in skill["links"])
    history = "".join(version_html(skill, skill["versions"], i)
                      for i in reversed(range(len(skill["versions"]))))
    return f"""<article class="portal wrap" aria-labelledby="skill-title">
  <nav class="crumbs" aria-label="Breadcrumb"><a href="../">Skills</a></nav>
  <h1 id="skill-title" class="fit">{esc(skill["name"])}</h1>
  <p class="lede">{esc(skill["summary"])}</p>
  {SAMPLE_NOTE if sample else ""}
  <p class="why">{risk_badge(current["risk"])} {esc(risk_why(current["risk"], current["permissions"]))}</p>
  <dl class="facts">
    <div><dt>Publisher</dt><dd><a href="https://github.com/{esc(skill["author"])}" rel="nofollow noopener">{esc(skill["author"])}</a></dd></div>
    <div><dt>Version</dt><dd>{esc(current["version"])}, {esc(current["date"][:10])}</dd></div>
    <div><dt>Licence</dt><dd>{esc(skill["licence"])}</dd></div>
    <div><dt>Id</dt><dd><code>{esc(skill["id"])}</code></dd></div>
    <div><dt>Tags</dt><dd class="chips">{tag_chips(skill["tags"], "../")}</dd></div>
    {f'<div><dt>Links</dt><dd class="links">{links}</dd></div>' if links else ""}
  </dl>
  <div class="buttons"><a class="btn" href="#install">Install</a><a class="btn" href="{esc(report_url(skill, current))}" rel="nofollow noopener">Report this skill</a></div>
  <section class="section" aria-labelledby="does-title">
    <h2 id="does-title">What it does</h2>
    <p class="muted">{esc(reach_words(current["permissions"]))} Each action below is one the model can choose, by the words it sees.</p>
    {actions}
  </section>
  {install_html(skill, current)}
  <section class="section" aria-labelledby="file-title">
    <h2 id="file-title">The whole recipe</h2>
    <p class="file-meta">{esc(skill["file_name"])}, version {esc(current["version"])}, {current["size"]} bytes. SHA-256 {esc(current["sha256"])}.</p>
    <pre class="code">{code(current["text"])}</pre>
  </section>
  <section class="section" aria-labelledby="versions-title">
    <h2 id="versions-title">Versions</h2>
    <ol class="history" reversed>{history}</ol>
  </section>
</article>"""


def build(dist: Path, folder: Path, shell) -> int:
    """Writes skills/, skills/<id>/ and skills/recipes/. shell(title, description, depth, body,
    sample) wraps a body in the site's page. Returns how many skill pages it wrote."""
    registry = load(folder)
    out = dist / "skills"
    (out / "recipes").mkdir(parents=True)
    sample = registry["sample"]
    (out / "index.html").write_text(shell(
        "Skills", "Recipe skills people share for Tuxistant.", 1, browse_html(registry), sample))
    for skill in registry["skills"]:
        page = out / skill["id"] / "index.html"
        page.parent.mkdir()
        page.write_text(shell(skill["name"], skill["summary"], 2, skill_html(skill, sample),
                              sample))
        for version in skill["versions"]:
            target = out / "recipes" / f"{version['sha256']}.yaml"
            target.write_bytes(version["bytes"])
            # The signature the index carries, which is the one Tux checks.
            target.with_name(target.name + ".sig").write_text(version["sig"])
    return len(registry["skills"])
