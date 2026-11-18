"""The skills portal. Everything in the registry is a stranger's text, so the tests feed it hostile
and broken indexes and check what reaches the page; the sample registry shows the rest."""
import base64
import contextlib
import copy
import hashlib
import importlib.util
import io
import json
import re
import shutil
import tempfile
import unittest
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SAMPLE = ROOT / "website/src/registry-sample"
TIDES = "io.github.example-ada.tides"


def _module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


build = _module("site_build", ROOT / "website/build.py")
portal = build._portal()


class Registry:
    """A copy of the sample registry that a test can change."""

    def __init__(self, test):
        folder = tempfile.TemporaryDirectory()
        test.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.folder = self.root / "registry"
        shutil.copytree(SAMPLE, self.folder)
        self.index = json.loads((self.folder / "index.json").read_text())
        self.revocations = json.loads((self.folder / "revocations.json").read_text())

    def skill(self, skill_id):
        return next(s for s in self.index["skills"] if s["id"] == skill_id)

    def recipe(self, skill_id, position=-1):
        version = self.skill(skill_id)["versions"][position]
        return (self.folder / "recipes" / f"{version['sha256']}.yaml").read_text()

    def replace_recipe(self, skill_id, text, position=-1):
        """New bytes for a version, with the hash and size the index gives for it."""
        data = text.encode()
        version = self.skill(skill_id)["versions"][position]
        version["sha256"] = hashlib.sha256(data).hexdigest()
        version["size"] = len(data)
        (self.folder / "recipes" / f"{version['sha256']}.yaml").write_bytes(data)

    def open(self):
        """As the real registry will be: no SAMPLE file."""
        (self.folder / "SAMPLE").unlink()
        return self

    def build(self):
        (self.folder / "index.json").write_text(json.dumps(self.index))
        (self.folder / "revocations.json").write_text(json.dumps(self.revocations))
        dist = self.root / "dist"
        shutil.rmtree(dist, ignore_errors=True)
        dist.mkdir()
        shell = build.portal_shell(build.load_languages()["en"], "default-src 'none'")
        portal.build(dist, self.folder, shell)
        return dist / "skills"


class BuiltSite(unittest.TestCase):
    """The whole site, built with the portal from a registry folder, as build.py --portal does."""

    @classmethod
    def setUpClass(cls):
        folder = tempfile.TemporaryDirectory()
        cls.addClassCleanup(folder.cleanup)
        cls.plain = Path(folder.name) / "dist"
        cls.dist = Path(folder.name) / "dist-portal"
        saved = build.DIST, build.DIST_PORTAL
        build.DIST, build.DIST_PORTAL = cls.plain, cls.dist
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                build.main(["--portal", str(cls.registry_in(Path(folder.name)))])
        finally:
            build.DIST, build.DIST_PORTAL = saved
        cls.skills = cls.dist / "skills"
        cls.pages = sorted(cls.skills.glob("**/index.html"))
        cls.index = json.loads((SAMPLE / "index.json").read_text())

    @classmethod
    def registry_in(cls, folder):
        return SAMPLE

    def page(self, skill_id=None):
        return (self.skills / (skill_id or "") / "index.html").read_text()


class BuiltPortal(BuiltSite):
    """Built from the sample registry, as the site is until the registry exists."""

    def front_pages(self):
        return [p for p in self.dist.glob("**/index.html") if "skills" not in p.parts]

    def test_it_is_written_beside_the_real_site_and_marked_for_deploy_to_refuse(self):
        self.assertFalse(self.plain.exists())
        self.assertTrue((self.dist / "LOCAL-PORTAL").is_file())
        self.assertTrue((self.dist / "skills.js").is_file())
        self.assertEqual((self.dist / "robots.txt").read_text(), "User-agent: *\nDisallow: /\n")
        self.assertIn("dist-portal/", (ROOT / "website/.gitignore").read_text().split())

    def test_a_page_per_skill_that_isnt_revoked(self):
        shown = {p.parent.name for p in self.pages if p.parent != self.skills}
        self.assertEqual(shown, {s["id"] for s in self.index["skills"]}
                         - {"io.github.example-eve.weather-now"})
        browse = self.page()
        for skill_id in shown:
            self.assertIn(f'href="{skill_id}/"', browse)
        self.assertNotIn("weather-now", browse)
        self.assertNotIn("Weather now", browse)

    def test_every_page_says_it_is_a_sample_while_the_sample_is_used(self):
        for page in self.pages:
            html = page.read_text()
            self.assertIn('class="sample"', html, page)
            self.assertIn('<meta name="robots" content="noindex">', html, page)

    def test_portal_pages_keep_the_sites_security_policy(self):
        policy = re.search(r'http-equiv="Content-Security-Policy" content="([^"]+)"',
                           (self.dist / "index.html").read_text())[1]
        for page in self.pages:
            html = page.read_text()
            self.assertIn(f'content="{policy}"', html, page)
            self.assertLess(html.index("Content-Security-Policy"), html.index("<script"), page)
            for script in re.findall(r"<script>(.*?)</script>", html, re.S):
                digest = base64.b64encode(hashlib.sha256(script.encode()).digest()).decode()
                self.assertIn(f"'sha256-{digest}'", policy, page)
            sources = re.findall(r'<script src="([^"]+)"', html)
            self.assertEqual([s.rsplit("/", 1)[-1] for s in sources], ["site.js", "skills.js"])
            self.assertNotRegex(html, r'<(script|link)[^>]+(src|href)="(https?:)?//', page)
            self.assertNotIn(" style=", html, page)
            self.assertNotRegex(html, r"""(href|src)=["']/(?!/)""", page)

    def test_portal_pages_have_the_sites_header(self):
        for page in self.pages:
            html = page.read_text()
            self.assertIn('<header class="site-header" id="site-header">', html, page)
            self.assertIn('<div class="nav-glass" aria-hidden="true"></div>', html, page)
            self.assertIn('aria-current="page"', html, page)

    def test_front_pages_link_to_the_portal_in_this_build_only(self):
        self.assertEqual(len(self.front_pages()), 5)
        for page in self.front_pages():
            html = page.read_text()
            self.assertIn('<header class="site-header" id="site-header">', html, page)
            root = "" if page.parent == self.dist else "../"
            self.assertIn(f'<a class="nav-link" href="{root}skills/">', html, page)
            self.assertIn('<span class="lang-pick">', html, page)

    def test_sample_pages_offer_nothing_to_install_or_report(self):
        self.assertNotIn("Report this skill", self.page(TIDES))
        self.assertNotIn("issues/new", self.page(TIDES))
        for page in self.pages:
            html = page.read_text()
            for offer in ("sha256sum", "ssh-keygen", "download=", "recipes/", "~/.local"):
                self.assertNotIn(offer, html, page)
        self.assertFalse((self.skills / "recipes").exists())
        self.assertIn("nothing to install or report", self.page(TIDES))
        for page in self.pages:
            self.assertNotIn('href="#install"', page.read_text(), page)
            self.assertNotIn('class="buttons"', page.read_text(), page)
        self.assertIn("Install will show you a card", self.page(TIDES))
        self.assertNotIn("Install shows you", self.page(TIDES))

    def test_browse_doesnt_say_the_registry_works_today(self):
        lede = re.search(r'<p class="lede">([^<]+)</p>', self.page())[1]
        self.assertEqual(lede, "Recipe skills people will share for Tuxistant. Each will be signed "
                               "by its publisher and reviewed before it's merged, and Tux will "
                               "check it again before installing.")

    def test_no_script_builds_markup(self):
        for name in ("site.js", "skills.js", "app.js"):
            script = (self.dist / name).read_text()
            self.assertNotRegex(script, r"innerHTML|outerHTML|insertAdjacentHTML|document\.write"
                                        r"|\beval\(|new Function|srcdoc", name)
        self.assertIn("textContent", (self.dist / "skills.js").read_text())

    def test_code_wraps_rather_than_hiding_a_line_off_to_the_side(self):
        css = (self.dist / "style.css").read_text()
        self.assertRegex(css, r"\.code \{[^}]*white-space: pre-wrap;[^}]*overflow-wrap: anywhere")

    def test_a_long_word_from_the_index_breaks_rather_than_running_off_the_page(self):
        css = (self.dist / "style.css").read_text()
        self.assertRegex(css, r"\n\.portal \{[^}]*overflow-wrap: anywhere;")
        self.assertRegex(css, r"\nh1, h2 \{[^}]*white-space: nowrap;", "headings shrink instead")
        # Everything a stranger writes is inside .portal, so the rule reaches it.
        registry = Registry(self)
        tides = registry.skill(TIDES)
        tides["summary"] = "S" * 140
        registry.index["revocations"][0]["reason"] = "R" * 300
        skills = registry.build()
        for page in (skills / "index.html", skills / TIDES / "index.html"):
            html = page.read_text()
            main = re.search(r"<main>(.*)</main>", html, re.S)[1]
            self.assertRegex(main.strip(), r'^<(section|article) class="portal wrap"', page)
            self.assertEqual(main.count('class="portal'), 1, page)
            self.assertIn("S" * 140, main, page)
        self.assertIn("R" * 300, (skills / TIDES / "index.html").read_text())

    def test_each_action_shows_the_description_the_model_sees(self):
        tides = self.page(TIDES)
        self.assertIn('<p class="model-text">Say when the next high tide is at a seaside town</p>',
                      tides)
        self.assertIn("<code>town</code>: The town, like Brighton <span class=\"muted\">"
                      "(words, up to 40 characters; needed)</span>", tides)
        lamp = self.page("io.github.example-cy.desk-lamp")
        self.assertIn("one of reading, warm, bright, off; needed", lamp)
        boiler = self.page("io.github.example-cy.boiler")
        self.assertIn("a whole number from 10 to 120; if left out, 30", boiler)

    def test_what_each_action_does_and_whether_it_asks_first(self):
        self.assertIn("Sends GET https://tides.example.org/v1/next?town={town} to "
                      "tides.example.org. Tux shows you a card with the exact request first, as "
                      "words you say go into it.", self.page(TIDES))
        self.assertIn("Tux shows you a card with the exact request every time.",
                      self.page("io.github.example-cy.desk-lamp"))
        self.assertIn("No card in a fresh conversation", self.page("io.github.example-dee.fuel"))
        bluetooth = self.page("io.github.example-bo.bluetooth")
        self.assertIn("Opens the Bluetooth page of Settings. No card.", bluetooth)
        self.assertIn("Reaches no network.", bluetooth)
        self.assertIn('<span class="risk risk-low">Low risk</span> It reaches no network', bluetooth)
        self.assertIn('<span class="risk risk-medium">Medium risk</span> It sends requests over '
                      "the network, to tides.example.org and nowhere else.", self.page(TIDES))

    def test_the_whole_recipe_is_on_its_page(self):
        version = self.index["skills"][0]["versions"][-1]
        recipe = (SAMPLE / "recipes" / f"{version['sha256']}.yaml").read_text()
        self.assertIn(f'<pre class="code">{recipe.replace(chr(34), "&quot;")}</pre>',
                      self.page(TIDES))

    def test_versions_show_what_changed_in_plain_words_and_as_a_diff(self):
        tides = self.page(TIDES)
        self.assertIn("<li>New host: tides.example.org</li>", tides)
        self.assertIn("<li>No longer reaches api.tides.example</li>", tides)
        self.assertRegex(tides, r'<span class="del">-    get: https://api\.tides\.example/')
        self.assertRegex(tides, r'<span class="add">\+    get: https://tides\.example\.org/')
        lamp = self.page("io.github.example-cy.desk-lamp")
        self.assertIn("The model sees a new description for lamp.scene", lamp)
        self.assertIn("The model can send different values to lamp.scene", lamp)

    def test_a_suspended_version_is_listed_with_its_reason(self):
        tides = self.page(TIDES)
        self.assertIn("<strong>Suspended:</strong> Its host, api.tides.example, has been taken "
                      "over by someone else.", tides)
        self.assertIn("<dd>1.1.0, 2026-11-20</dd>", tides, "the newest version still allowed")


class OpenRegistry(BuiltSite):
    """Built from the registry as it will be, with no SAMPLE file."""

    @classmethod
    def registry_in(cls, folder):
        shutil.copytree(SAMPLE, folder / "registry")
        (folder / "registry" / "SAMPLE").unlink()
        return folder / "registry"

    def test_no_page_says_it_is_a_sample(self):
        for page in self.pages:
            html = page.read_text()
            self.assertNotIn('class="sample"', html, page)
            self.assertNotIn("noindex", html, page)
        self.assertIn("Each is signed by its publisher and reviewed before it's merged",
                      self.page())

    def test_report_opens_the_registry_issue_form_filled_in(self):
        link = re.search(r'<a class="btn" href="([^"]+)"[^>]*>Report this skill</a>',
                         self.page(TIDES))[1]
        url = urllib.parse.urlsplit(link.replace("&amp;", "&"))
        self.assertEqual((url.scheme, url.netloc, url.path),
                         ("https", "github.com", f"/{portal.REGISTRY_REPO}/issues/new"))
        query = dict(urllib.parse.parse_qsl(url.query))
        version = self.index["skills"][0]["versions"][-1]
        self.assertEqual(query["template"], "report.yml")
        self.assertEqual((query["skill"], query["version"], query["sha256"]),
                         (TIDES, "1.1.0", version["sha256"]))

    def test_install_by_hand_checks_the_hash_and_the_signature(self):
        version = self.index["skills"][0]["versions"][-1]
        key = self.index["authors"]["example-ada"]["keys"][0]["key"]
        tides = self.page(TIDES)
        self.assertIn(f"echo &#x27;{version['sha256']}  tides.yaml&#x27; | sha256sum -c", tides)
        self.assertIn(f"echo &#x27;io.github.example-ada {key}&#x27; &gt; tides.signers", tides)
        self.assertIn("ssh-keygen -Y verify -f tides.signers -I io.github.example-ada -n "
                      "tuxistant-recipe -s tides.yaml.sig &lt; tides.yaml", tides)
        served = self.skills / "recipes" / f"{version['sha256']}.yaml"
        self.assertEqual(hashlib.sha256(served.read_bytes()).hexdigest(), version["sha256"])
        self.assertEqual(served.with_name(served.name + ".sig").read_text(), version["sig"])
        self.assertIn('href="#install"', tides)
        self.assertIn("Install shows you a card", tides)

    def test_only_versions_tux_would_install_are_served(self):
        served = {p.name for p in (self.skills / "recipes").iterdir()}
        suspended = self.index["skills"][0]["versions"][0]       # tides 1.0.0
        self.assertEqual(suspended["version"], "1.0.0")
        self.assertNotIn(f"{suspended['sha256']}.yaml", served)
        self.assertNotIn(f"{suspended['sha256']}.yaml.sig", served)
        weather = next(s for s in self.index["skills"]
                       if s["id"] == "io.github.example-eve.weather-now")
        self.assertNotIn(f"{weather['versions'][0]['sha256']}.yaml", served)
        current = self.index["skills"][0]["versions"][-1]
        self.assertIn(f"{current['sha256']}.yaml", served)


class HostileRegistry(unittest.TestCase):
    def test_strings_from_the_index_and_the_recipe_are_escaped(self):
        registry = Registry(self)
        tides = registry.skill(TIDES)
        tides["name"] = 'Tides & "Co" \'x\''
        tides["links"] = [{"title": 'A "quoted" & link', "url": "https://example.org/?a=1&b=2"}]
        registry.replace_recipe(TIDES, registry.recipe(TIDES).replace(
            'name: Tides', 'name: Tides & "Co" \'x\'') +
            "# </pre><script>alert(1)</script><img src=x onerror=alert(2)>\n")
        page = (registry.build() / TIDES / "index.html").read_text()
        self.assertNotIn("<script>alert", page)
        self.assertNotIn("<img", page)
        self.assertIn("&lt;/pre&gt;&lt;script&gt;alert(1)&lt;/script&gt;", page)
        self.assertIn("Tides &amp; &quot;Co&quot; &#x27;x&#x27;", page)
        self.assertIn('href="https://example.org/?a=1&amp;b=2"', page)
        self.assertIn("A &quot;quoted&quot; &amp; link", page)
        self.assertNotIn('"Co"', page)

    def test_hidden_characters_in_a_recipe_are_shown_by_their_code(self):
        registry = Registry(self)
        registry.replace_recipe(TIDES, registry.recipe(TIDES) + "# safe ‮;evil\n")
        page = (registry.build() / TIDES / "index.html").read_text()
        self.assertNotIn("‮", page)
        self.assertIn('<span class="hidden-char" title="A hidden character">U+202E</span>', page)

    def test_a_file_that_isnt_the_one_the_index_names_refuses_the_build(self):
        registry = Registry(self)
        version = registry.skill(TIDES)["versions"][-1]
        file = registry.folder / "recipes" / f"{version['sha256']}.yaml"
        file.write_text(file.read_text().replace("tides.example.org/v1", "tides.example.org/v2"))
        with self.assertRaisesRegex(portal.Refused, "isn't the one the index names"):
            registry.build()
        file.unlink()
        with self.assertRaisesRegex(portal.Refused, "missing"):
            registry.build()

    def test_a_recipe_that_loads_differently_from_its_entry_refuses_the_build(self):
        registry = Registry(self)
        registry.replace_recipe(TIDES, registry.recipe(TIDES).replace('version: "1.1.0"',
                                                                      'version: "9.9"'))
        with self.assertRaisesRegex(portal.Refused, "loader"):
            registry.build()

    def test_an_index_that_breaks_the_format_is_refused(self):
        def tides(field, value):
            return lambda index: index["skills"][0].__setitem__(field, value)

        cases = {
            "an id that leaves its folder": tides("id", "io.github.example-ada.../../x"),
            "an id under someone else's name": tides("author", "example-bo"),
            "a name with markup": tides("name", "Tides <b>"),
            "a name on two lines": tides("name", "Tides\nmore"),
            "a summary with a hidden character": tides("summary", "Tides ‮"),
            "a javascript link": tides("links", [{"title": "x", "url": "javascript:alert(1)"}]),
            "a link with a quote": tides("links", [{"title": "x", "url": 'https://a.example/"x'}]),
            "a tag nobody approved": tides("tags", ["weather", "hacking"]),
            "a licence that isn't allowed": tides("licence", "GPL-3.0"),
            "a field the format doesn't have": tides("rating", 5),
            "a risk Tux wouldn't compute": lambda index: index["skills"][0]["versions"][0]
            .__setitem__("risk", "low"),
            "a hash in the wrong form": lambda index: index["skills"][0]["versions"][0]
            .__setitem__("sha256", "../../etc/passwd"),
            "a key its author doesn't have": lambda index: index["skills"][0]["versions"][0]
            .__setitem__("key", "SHA256:" + "A" * 43),
            "a newer format": lambda index: index.__setitem__("format", 2),
            "a ban scoped to a skill": lambda index: index["revocations"][0]
            .__setitem__("kind", "ban"),
            "a report that isn't an issue": lambda index: index["revocations"][0]
            .__setitem__("report", "https://evil.example/"),
        }
        for name, change in cases.items():
            with self.subTest(name):
                registry = Registry(self)
                change(registry.index)
                with self.assertRaises(portal.Refused):
                    registry.build()

    def test_hosts_in_the_index_are_held_to_the_loaders_form(self):
        """Even for a file the loader refuses, where nothing compares them with the file's."""
        cases = {
            "a right-to-left override": ["moc.elpmaxe-yap\u202e.bank"],
            "a line break": ["two\nlines.example"],
            "markup": ["<b>x</b>.example"],
            "longer than a host name": [("a" * 60 + ".") * 5 + "example"],
            "a host named twice": ["tides.example.org", "tides.example.org"],
            "five hosts": [f"h{n}.example.org" for n in range(5)],
            "no hosts": [],
            "not a list": "tides.example.org",
        }
        bluetooth = "io.github.example-bo.bluetooth"
        for name, hosts in cases.items():
            with self.subTest(name):
                registry = Registry(self)
                registry.replace_recipe(bluetooth, registry.recipe(bluetooth) + "risk: low\n")
                version = registry.skill(bluetooth)["versions"][-1]
                version.update(permissions={"network": hosts}, risk="medium")
                with self.assertRaisesRegex(portal.Refused, "aren't 1 to 4 different host names"):
                    registry.build()
        registry = Registry(self)
        registry.replace_recipe(bluetooth, registry.recipe(bluetooth) + "risk: low\n")
        registry.skill(bluetooth)["versions"][-1].update(
            permissions={"network": ["Tides.example.org", "a-b.example.org"]}, risk="medium")
        page = (registry.build() / bluetooth / "index.html").read_text()   # host names: built
        self.assertNotIn("a-b.example.org", page, "only a file Tux loads backs up its hosts")

    def test_a_skill_whose_file_tux_refuses_has_no_card_and_nothing_to_install(self):
        registry = Registry(self).open()
        bluetooth = "io.github.example-bo.bluetooth"
        registry.replace_recipe(bluetooth, registry.recipe(bluetooth) + "risk: low\n")
        skills = registry.build()
        browse = (skills / "index.html").read_text()
        self.assertNotIn(f'href="{bluetooth}/"', browse)
        self.assertNotIn("Bluetooth page", browse)
        self.assertIn(f'href="{TIDES}/"', browse)
        self.assertIn(">5 skills</h2>", browse)
        page = (skills / bluetooth / "index.html").read_text()
        self.assertIn("Tux&#x27;s loader refuses this version, so it can&#x27;t be installed: ",
                      page)
        for offer in ('id="install"', 'href="#install"', "sha256sum", "download=", "Low risk",
                      "Reaches no network"):
            self.assertNotIn(offer, page)
        self.assertIn("Report this skill", page)
        refused = registry.skill(bluetooth)["versions"][-1]["sha256"]
        self.assertFalse((skills / "recipes" / f"{refused}.yaml").exists())
        self.assertFalse((skills / "recipes" / f"{refused}.yaml.sig").exists())
        tides = registry.skill(TIDES)["versions"][-1]["sha256"]
        self.assertTrue((skills / "recipes" / f"{tides}.yaml").exists())

    def test_revocations_hide_skills_until_the_index_lifts_them(self):
        registry = Registry(self)
        registry.index["lifted"] = []
        bo_first_key = registry.index["authors"]["example-bo"]["keys"][0]["fingerprint"]
        registry.index["revocations"].append(
            {"id": 4, "kind": "ban", "scope": {"key": bo_first_key}, "reason": "A leaked key.",
             "date": "2026-11-21T10:00:00Z", "actor": "example-mod", "report": None})
        # Only in revocations.json, as a suspension made since the index was signed.
        registry.revocations["revocations"].append(
            {"id": 5, "kind": "suspend", "scope": {"skill": "io.github.example-cy.desk-lamp"},
             "reason": "Being looked at.", "date": "2026-11-21T11:00:00Z",
             "actor": "example-mod", "report": None})
        skills = registry.build()
        shown = {p.parent.name for p in skills.glob("*/index.html")}
        self.assertEqual(shown, {TIDES, "io.github.example-bo.sound", "io.github.example-cy.boiler"})
        browse = (skills / "index.html").read_text()
        for hidden in ("Fuel prices", "Bluetooth page", "Desk lamp", "Weather now"):
            self.assertNotIn(hidden, browse)

    def test_revocations_json_cant_change_or_lift_what_the_index_says(self):
        registry = Registry(self)
        changed = copy.deepcopy(registry.index["revocations"][1])
        changed["scope"] = {"skill": "io.github.example-eve.weather-now", "versions": ["9"]}
        registry.revocations["revocations"] = [changed]
        shown = {p.parent.name for p in registry.build().glob("*/index.html")}
        self.assertNotIn("io.github.example-eve.weather-now", shown)


if __name__ == "__main__":
    unittest.main()
