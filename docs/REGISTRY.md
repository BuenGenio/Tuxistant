# The skills registry

Where people share recipe skills, and how Tux finds, installs, updates and removes them. This is the plan agreed with the owner on 2026-11-13. Nothing here is built yet. The exact file formats are in [REGISTRY-FORMAT.md](REGISTRY-FORMAT.md).

The plan starts from the same assumption as the rest of Tuxistant: a bad skill will eventually get past review. Review, signing and moderation make that rarer and shorter. What protects you when it happens is what Tux itself enforces: risk computed from permissions, a card for every risky action, recipes that can only reach the hosts they name, and no shell.

## Decisions

| | Decided |
|---|---|
| Infrastructure | A reviewed Git repo and a static site. No server, no accounts, nothing to keep running. A small API can sit beside it later for things a static site can't do |
| Trust | SSH signatures (`ssh-keygen -Y sign` / `-Y verify`). Every Linux system has them, so the daemon needs no new dependency, and a publisher can sign with the SSH key already on their GitHub account. Tux trusts a signature it checks itself and a hash it pinned, never the site |
| What can be shared | Recipes only. Code skills come later, once the sandbox has been seen working on real desktops |
| Publishers | A GitHub account. Skill ids are `io.github.<user>.<skill>`, so nobody can publish under someone else's name |
| Portal features | Browse and search, tags grouped into categories, a page per skill with its whole file, permissions, risk and version history with diffs, and install steps. No ratings or comments: GitHub Discussions does that |
| Installing | A Browse page in Settings, working the way GNOME's Extension Manager does |
| Bad skills | A revocation list inside the signed index, so Tux turns a revoked skill off by itself, and a moderation screen for acting on reports (see Moderation) |
| Review and licence | The owner reviews every pull request at first. Every skill is MIT or CC0. Nothing is announced until the first skills have been through review |
| Where it lives | GitHub Pages, under the project's site, until there is a custom domain |

## How a skill gets published

1. The publisher forks the registry repo and adds `skills/<github-user>/<skill>/recipe.yaml`, a `meta.yaml` (tags, licence, a short summary, links) and `recipe.yaml.sig`, an SSH signature made with a key listed on their GitHub account.
2. CI checks the pull request with Tux's own loader, the same code the daemon runs:
   - the recipe loads with no problem;
   - its id is `io.github.<the PR author>.*`;
   - the signature verifies against a key GitHub lists for the PR author;
   - an update is signed with a key the author already had on `main`, and a key added once they have a skill published is vouched for by one of their earlier keys, or else the owner confirms it is them;
   - the licence is MIT or CC0, and the tags exist;
   - the author isn't suspended or banned.

   For an update, CI also prints what changed in plain words: new hosts, new permissions, a new action, a changed description the model sees. These are what a reviewer reads first.
3. The owner reviews it. A reviewer reads the whole file; a recipe is short enough for that.
4. On merge, CI builds the index and the site, and the registry key signs the index.

## What Tux downloads

One file, `index.json`, with `index.json.sig` beside it, signed by the registry key. The registry key's public half ships with Tux. The index holds:

- every skill version: id, version, the SHA-256 of `recipe.yaml`, the publisher's signature, the permissions and hosts, the risk Tux will compute, tags, summary and date;
- the tag and category lists;
- the revocation list (see Moderation);
- a sequence number and a date, so Tux refuses an older index than one it has already seen. Without that, someone could serve an old index to hide a revocation.

Tux checks the index signature, then, for each install or update, downloads `recipe.yaml`. It checks it against the hash in the index and the publisher's signature, then runs it through its normal loader. A skill that fails any of these is not installed, and Settings says why.

The site and its host are never trusted. If GitHub Pages served something else, the signatures would fail.

## Keys

| Key | Held by | Signs | If it leaks |
|---|---|---|---|
| Publisher key | Each publisher, on their own machine | Their own recipes | Someone can publish as them until they remove the key from GitHub. CI then refuses new versions signed with it, and moderation can suspend their skills |
| Registry key | The owner, offline | The index, at each merge | Serious: a new key ships with the next Tuxistant release. Mitigated because Tux still checks each recipe's publisher signature and pinned hash |
| Moderation key | CI, as a secret | Revocation entries only | Someone can switch skills off, but never add or change one. Switching off is the safe direction, so this key can live where moderation needs to be fast |

## Logos

A skill can have a logo, still or animated: PNG and APNG, WebP, AVIF or GIF, square, up to 512 pixels and 512 KiB, signed by the publisher like the recipe. [REGISTRY-FORMAT.md](REGISTRY-FORMAT.md#logos) has the exact rules.

- **No SVG.** An SVG can carry scripts and links to other servers. The raster formats are only pixels.
- **Decoded in a sandbox.** Image decoders are a common way in, so Settings and Browse decode logos with glycin, GNOME's image library. It runs each decoder in a sandbox of its own, not in Settings. The daemon never decodes a logo, and the model never sees one.
- **Checked before it is shown.** Tux shows a logo only when its hash matches the index and the publisher's signature verifies, as for a recipe.
- **Calm by default.** A logo animates only while its skill's page is open and only if animations are on in GNOME's settings. Lists and everyone with animations off see the still first frame. CI refuses a logo that flashes more than three times a second.
- **Built-in skills get logos too.** They are drawn for Tuxistant as one set, so the Skills page looks the same with or without shared skills.
- **Moderation covers logos.** The report form has "the logo is misleading or offensive", and suspending a skill hides its logo too.

## Tags and categories

A tag is one word or short phrase: `weather`, `smart-home`, `prices`, `transport`, `notes`. A category is a named collection of tags, chosen by the registry, not by publishers: "Home" holds `smart-home`, `lights` and `heating`, and "Information" holds `weather`, `prices` and `transport`. Publishers pick tags from the list in `tags.yaml`. A new tag is proposed in the same pull request and approved with it. The portal and the Browse page show categories first, then tags inside them.

## Browse in Settings

A Browse page, close to GNOME's Extension Manager:

- **Search and categories.** A search box, the categories, and the newest and most installed skills. Installs aren't counted at first, as counting needs a server.
- **A skill's page.** Its summary, publisher, tags, licence and risk with the reason. It shows its permissions and hosts in plain words, each action's description exactly as the model will see it, and the whole `recipe.yaml`. These are the same views the Skills page has for installed skills.
- **Install.** It shows the approval card with the hosts and risk. On Allow it downloads and checks the recipe, then writes it into `~/.local/share/tuxistant/skills/`, where the loader picks it up.
- **Updates.** Tux checks the index once a day, with no identifier sent. An update that asks for nothing new installs the same way. One with a new host, permission or action waits for a card. Tux won't update a skill you have edited.
- **Remove.** It removes the file.
- **Revoked skills.** They are switched off at once, with a notification that names the reason from the revocation list.

Nothing about you is sent to the registry. Fetching the index is the only request, and it is the same for everyone.

## Moderation

### Reports

Anyone can report a skill:

- **from Settings:** a Report button on the skill's page opens a GitHub issue form, with the skill id, version and hash already filled in;
- **from the portal:** the same form, linked from every skill page;
- **from GitHub:** the same issue template.

Every report gets the `report` label and a link to the skill's file at that exact version. Reports are public, like the rest of the registry. For something that shouldn't be public, such as a skill that steals data still in use, the form points to GitHub's private vulnerability reporting instead.

### The moderation screen

A page on the portal, `/moderation/`. It is a static view built from public data:

- open reports, newest first, grouped by skill and by author;
- for each author: their GitHub account, how old it is, their skills and versions, past reports and past actions;
- for each skill: what changed in each version, in the same plain words CI uses;
- the full action log.

Its buttons don't do anything themselves. Each opens a GitHub Actions run (`workflow_dispatch`) with the inputs filled in, so GitHub checks that the person pressing it is a moderator with rights on the repo. The site needs no logins of its own.

### Actions

| Action | What happens | What Tux does |
|---|---|---|
| Suspend skill | All versions, or chosen ones, go on the revocation list with a reason. The skill is hidden from Browse and the portal | Switches it off and says why. It stays on disk, so if it is reinstated it comes back on |
| Suspend author | All their skills are suspended, and CI refuses new pull requests from them until it is lifted | As for each skill |
| Ban author | Their skills are revoked for good, their keys are refused, and CI refuses them | Switches the skills off and offers to remove them |
| Contact | Opens an issue that mentions the author, from a template: what was reported, what they need to change, by when | Nothing |
| Reinstate | Undoes a suspension, with a reason | Switches it back on |
| Dismiss report | Closes the report with a reason | Nothing |

Each action is a commit to the registry repo by the workflow, signed with the moderation key. The commit records who, what, why and which report, so the action log is the repo's history and anyone can read it.

Suspending is fast: one button and one CI run, and every Tux picks it up at its next daily check. A user who wants it sooner can press "Check now". Banning and reinstating need a second look: the workflow opens a pull request that the owner merges.

### Appeals

An author answers on the Contact issue or opens an `appeal` issue. A moderator other than the one who acted, once there is more than one, decides, and the decision goes in the log.

### Rules for moderators

Use Suspend while looking into a report, since it costs a user only a skill that is off for a while. Ban for proven malice: a skill that sends private data somewhere undeclared, a publisher who impersonates someone, or a second offence after Contact. Never edit a publisher's recipe: suspend it and ask them to fix it.

## Building it, in order

1. **The registry repo**, with the layout above, CI running Tux's loader as a check, signature checks against GitHub's published keys, the index build, and `tags.yaml` and categories. Three example recipes from the owner.
2. **Signing in Tux:** checking the index and recipe signatures with `ssh-keygen -Y verify`, the pinned hashes, the sequence number and the revocation list. All of it is tested offline.
3. **The portal:** browse, search, categories and skill pages, built into the existing GitHub Pages site.
4. **Browse in Settings:** search, skill pages, install, update, remove, Report.
5. **Moderation:** the report form, the moderation screen, and the workflows behind each action.
6. **A private trial:** a handful of people publish and install through it before any announcement.

## Not decided yet

- The name of the registry repo, such as `BuenGenio/tuxistant-skills`. Creating it is a public step, so it waits for the owner.
- Who moderates besides the owner, and when there are enough people for appeals to go to someone else.
- Whether install counts are worth a small server. They would be the first thing the static site can't do.
- Code skills in the registry: what review, sandbox evidence and signing they would need.
