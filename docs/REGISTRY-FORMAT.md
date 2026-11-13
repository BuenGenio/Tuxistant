# Registry file formats

The exact files behind [REGISTRY.md](REGISTRY.md): what the registry repo holds, what its CI publishes, what each key signs, and how Tux decides what to trust. The repo's CI, the index build, the portal, signing in Tux and Browse in Settings all build to this page. Change it here first.

A signed example of everything Tux downloads is in [examples/registry/](examples/registry/). Its private keys were thrown away.

## Conventions

| Term | Meaning |
|---|---|
| login | A GitHub login in small letters: 1 to 39 letters, digits and single hyphens, not starting or ending with a hyphen. CI lowercases the PR author's login before comparing |
| skill name | 1 to 40 small letters, digits and hyphens, starting with a letter or digit |
| skill id | `io.github.<login>.<skill name>`, exactly four parts |
| text(n) | A string of 1 to n characters on one line: no control or format characters, no `<` or `>`, no space at either end. The same rule recipes use for `name` |
| tag | 2 to 24 characters: small letters and digits in groups joined by single hyphens, such as `smart-home` |
| timestamp | UTC, to the second: `2026-11-20T09:00:00Z` |
| day | UTC: `2026-11-20` in JSON, `20261120Z` in `allowed_signers` |
| key | An SSH public key as two fields, `<type> <base64>`, with no comment. Publishers may use `ssh-ed25519`, `sk-ssh-ed25519@openssh.com`, `ecdsa-sha2-nistp256`, `-nistp384`, `-nistp521`, `sk-ecdsa-sha2-nistp256@openssh.com`, or `ssh-rsa` of at least 2048 bits. The registry and moderation keys are `ssh-ed25519` |
| fingerprint | As `ssh-keygen -l` prints it: `SHA256:` and 43 base64 characters |

YAML files in the repo are read the way Tux reads recipes: safe loading, no aliases, and any field not listed here refuses the file with a reason.

## The registry repo

```text
README.md, CONTRIBUTING.md, LICENSE      the repo's own; each skill has its licence in meta.yaml
.gitattributes                           skills/** -text, so git never changes a signed file's bytes
.github/workflows/                       PR check, index build, deploy, moderation actions
.github/ISSUE_TEMPLATE/                  report, appeal, contact
tags.yaml                                tags and categories
keys/registry.pub                        the registry key's public half, as Tux ships it
keys/moderation.pub                      the moderation key's public half
authors/<login>/allowed_signers          the keys that may sign <login>'s recipes, with dates
skills/<login>/<skill name>/recipe.yaml
skills/<login>/<skill name>/recipe.yaml.sig
skills/<login>/<skill name>/meta.yaml
moderation/revocations/<id>.yaml         one per revocation, never edited or deleted
moderation/lifted/<id>.yaml              one per lifted revocation, same id
published/index.json                     committed by the owner with its signature
published/index.json.sig
published/revocations.json               committed by the moderation workflow with its signature
published/revocations.json.sig
```

A publisher's pull request may touch only `skills/<login>/`, `authors/<login>/allowed_signers` and, to propose a tag, `tags.yaml`, where `<login>` is the PR author. `moderation/` and `published/` change only through the moderation workflow or the owner. Pull requests are merged with squash or a merge commit, never rebased, so each first-parent commit on `main` is one reviewed change.

## recipe.yaml and recipe.yaml.sig

`recipe.yaml` is a recipe as in "Recipe skills" in [SKILLS.md](SKILLS.md), at most 64 KiB, and it must load with Tux's own loader. In the registry it also needs:

- `id` equal to `io.github.<login>.<skill name>` from its folder;
- a `version` of 1 to 40 characters from `A-Z a-z 0-9 . + -`, starting with a letter or digit, different from every earlier version of that skill. Versions are ordered by when they were merged, not by their numbers.

`recipe.yaml.sig` is made by the publisher with

```text
ssh-keygen -Y sign -f <their key> -n tuxistant-recipe recipe.yaml
```

over the file's exact bytes. It is the armored `-----BEGIN SSH SIGNATURE-----` text, at most 4 KiB.

## meta.yaml

```yaml
summary: Says when the next high tide is at a seaside town
tags: [weather, transport]
licence: MIT
links:
  - title: Source
    url: https://github.com/example-ada/tides
```

| Field | Rule |
|---|---|
| `summary` | Required. text(140). Shown in Browse and on the portal; the model never sees it |
| `tags` | Required. 1 to 4 tags from `tags.yaml`, no repeats |
| `licence` | Required. Exactly `MIT` or `CC0-1.0` |
| `links` | Optional, 0 to 3. Each has `title`, text(30), and `url`: `https://`, at most 200 characters, no spaces, no user or password |

At most 4 KiB.

## tags.yaml

```yaml
categories:
  - id: home
    name: Home
    tags: [smart-home, lights, heating]
  - id: information
    name: Information
    tags: [weather, prices, transport]
  - id: desktop
    name: Desktop
    tags: [settings, apps]
```

`categories` is the only field: 1 to 30 categories, in the order Browse shows them. Each has an `id` (a tag-shaped word), a `name`, text(30), and 1 to 30 `tags`. Every tag is in exactly one category. A tag is shown as it is written. A new tag is added here in the pull request that first uses it.

## authors/&lt;login&gt;/allowed_signers

The keys that may sign `<login>`'s recipes, in the format `ssh-keygen -Y verify -f` reads, one line per key ever accepted, oldest first, never deleted:

```text
io.github.example-ada namespaces="tuxistant-recipe",valid-after="20261114Z" ssh-ed25519 AAAAC3Nza...
io.github.example-ada namespaces="tuxistant-recipe",valid-after="20250301Z",valid-before="20261114Z" ssh-rsa AAAAB3Nza...
```

The principal is always `io.github.<login>` and the namespace always `tuxistant-recipe`. No other options and no comments. At most 50 lines.

How CI fills and checks it, on every pull request that touches `skills/<login>/`:

1. It fetches `https://github.com/<login>.keys` for the PR author. These are the only keys that can be added.
2. Each `recipe.yaml.sig` in the pull request must verify with `ssh-keygen -Y verify -f authors/<login>/allowed_signers -I io.github.<login> -n tuxistant-recipe`, at the current time, and with a key GitHub lists right now.
3. A key GitHub lists that the file doesn't have is added by the pull request with `valid-after` set to the day CI first saw it. A listed key GitHub no longer has gets `valid-before` set to the day CI found it gone. CI prints the exact lines and refuses the pull request until they are there, so the reviewer sees every key change.

So a key removed from GitHub stops new versions at once, while versions it signed earlier stay valid until moderation acts on them.

## moderation/revocations/&lt;id&gt;.yaml and moderation/lifted/&lt;id&gt;.yaml

A revocation file holds one revocation entry (see "Revocations" below) in YAML, with `id` equal to its file name. Ids are whole numbers from 1, the next one being the highest so far plus 1, and never reused. The moderation workflow writes a suspension straight to `main`, and opens a pull request for a ban.

A lifted file holds one lift entry, and only the owner merges one. Lifting is how a suspension or ban is undone; the revocation file stays.

## What is published

Everything is served from one base address that ships with Tux, such as `https://<pages host>/registry/`:

| Path | What | Signed by |
|---|---|---|
| `index.json`, `index.json.sig` | Copied from `published/` | Registry key, namespace `tuxistant-index` |
| `revocations.json`, `revocations.json.sig` | Copied from `published/` | Moderation key, namespace `tuxistant-revocations` |
| `recipes/<sha256>.yaml`, `recipes/<sha256>.yaml.sig` | Every version in the published `index.json`, named by the SHA-256 of its bytes, in small hex | Its publisher, namespace `tuxistant-recipe` |

Tux downloads `index.json`, `revocations.json`, their signatures, and `recipes/<sha256>.yaml` when it installs or updates. It takes a recipe's signature from the index; the `.sig` file beside it is for people checking by hand.

## index.json

UTF-8 JSON, at most 4 MiB. Its signature is at most 4 KiB.

The build takes a commit of `main`, a `seq` and a `date`, and is otherwise deterministic: the same three give the same bytes, written with sorted keys, two-space indentation and a final newline. That lets the owner rebuild it offline, compare, and sign what they checked. CI refuses to deploy a `published/index.json` whose signature doesn't verify or which differs from its own rebuild.

### Top level

| Field | Type | Rule |
|---|---|---|
| `format` | integer | `1`. Tux refuses any other with "This needs a newer Tuxistant" |
| `seq` | integer | 1 or more. Each published index is exactly one more than the one before |
| `date` | timestamp | When it was built |
| `commit` | string | The 40-hex-digit commit of `main` it was built from |
| `moderation_keys` | list of key | 1 to 3 `ssh-ed25519` keys that may sign `revocations.json` |
| `categories` | list | As in `tags.yaml`: objects with `id`, `name`, `tags` |
| `authors` | object | By login, every author with at least one version. See below |
| `skills` | list | Every skill ever merged, sorted by id. See below |
| `revocations` | list | Every file in `moderation/revocations/`, sorted by id. See "Revocations" |
| `lifted` | list | Every file in `moderation/lifted/`, sorted by id. See "Revocations" |

### authors.&lt;login&gt;

| Field | Type | Rule |
|---|---|---|
| `status` | string | `active`, `suspended` or `banned`, worked out from the revocations and lifts in this index: `banned` if a ban scoped to the author is in effect, else `suspended` if a suspension scoped to the author is, else `active` |
| `keys` | list | One per line of `authors/<login>/allowed_signers`, in the same order |
| `keys[].key` | key | The key |
| `keys[].fingerprint` | fingerprint | Its fingerprint |
| `keys[].added` | day | Its `valid-after` |
| `keys[].removed` | day or null | Its `valid-before`, or null |

### skills[]

| Field | Type | Rule |
|---|---|---|
| `id` | skill id | |
| `author` | login | The login in the id |
| `name` | text(100) | The newest version's `name` |
| `summary`, `tags`, `licence`, `links` | | From `meta.yaml` at `commit` |
| `versions` | list | 1 or more, oldest first: one for each first-parent commit of `main` that changed `recipe.yaml` |
| `versions[].version` | string | The recipe's `version` |
| `versions[].date` | timestamp | The committer date of that commit |
| `versions[].sha256` | string | 64 small hex digits: SHA-256 of `recipe.yaml`'s exact bytes |
| `versions[].size` | integer | Its length in bytes, 1 to 65536 |
| `versions[].key` | fingerprint | The key that signed it, which must be in `authors.<author>.keys` |
| `versions[].sig` | string | The whole of `recipe.yaml.sig` at that commit, with its line breaks and final newline |
| `versions[].permissions` | object | `{}` or `{"network": [<1 to 4 hosts>]}`, exactly as the loader returns it. The hosts are what Browse lists |
| `versions[].risk` | string | `risk_of()` of those permissions: `low` without hosts, `medium` with them |

A skill whose folder is deleted keeps its entry, so installed copies can still be checked against it.

## Revocations

### A revocation entry

| Field | Type | Rule |
|---|---|---|
| `id` | integer | 1 or more, unique, never reused |
| `kind` | string | `suspend` or `ban`. A `ban` is scoped to an author or a key, never a skill |
| `scope` | object | Exactly one of the three forms below |
| `reason` | text(300) | Shown to the user in the notification and in Settings |
| `date` | timestamp | When the action was taken |
| `actor` | login | The moderator who took it |
| `report` | string or null | `https://github.com/<owner>/<repo>/issues/<number>` of the report, at most 200 characters, or null for an action taken without one |

| Scope | Covers |
|---|---|
| `{"skill": <id>}` | Every version of that skill |
| `{"skill": <id>, "versions": [<1 to 50 versions>]}` | Those versions of it |
| `{"author": <login>}` | Every version of every skill whose id starts `io.github.<login>.` |
| `{"key": <fingerprint>}` | Every version whose `key` is that fingerprint, by any author |

What Tux does: a covered skill is switched off with a notification giving the `reason`, and hidden from Browse. For a `suspend` it stays on disk and comes back on if the revocation is lifted. For a `ban` Tux also offers to remove it. Revocations only touch skills installed from the registry, matched by the id, version and key fingerprint Tux recorded when it installed them. A recipe someone wrote or copied in by hand is left alone.

### A lift entry

| Field | Type | Rule |
|---|---|---|
| `id` | integer | The id of the revocation it lifts |
| `reason` | text(300) | Why |
| `date` | timestamp | When |
| `actor` | login | Who merged it |
| `report` | string or null | As for a revocation |

### revocations.json

UTF-8 JSON, at most 1 MiB, with exactly these fields:

| Field | Type | Rule |
|---|---|---|
| `format` | integer | `1` |
| `seq` | integer | 1 or more, its own counter, one more each time it is published |
| `date` | timestamp | When it was signed |
| `revocations` | list | Every file in `moderation/revocations/` on `main`, sorted by id |

The moderation workflow publishes it after every change to `moderation/revocations/`, so a suspension reaches Tux without waiting for the owner to sign an index. A ban merged by the owner is published the same way.

## Signatures and keys

Every signature is `ssh-keygen -Y sign`, and Tux checks each with `ssh-keygen -Y verify` against an `allowed_signers` file it writes for that one check. The namespace is fixed per kind of file, so a signature made for one kind is refused for another.

| File | Namespace | Principal | Key |
|---|---|---|---|
| `recipe.yaml` | `tuxistant-recipe` | `io.github.<login>` | The publisher's, from the index |
| `index.json` | `tuxistant-index` | `tuxistant-registry` | The registry key, shipped with Tux |
| `revocations.json` | `tuxistant-revocations` | `tuxistant-moderation` | A key in the accepted index's `moderation_keys` |

| Key | May sign | What Tux takes from it |
|---|---|---|
| Publisher key | That publisher's `recipe.yaml` files | One recipe's bytes |
| Registry key | `index.json` | Everything in the index: skills, versions, hashes, publisher keys, tags, the moderation keys, revocations and lifts |
| Moderation key | `revocations.json`, and the moderation workflow's git commits (namespace `git`, for people reading the log, never read by Tux) | New revocation entries, and nothing else |

Tux ships the registry key as one `allowed_signers` line:

```text
tuxistant-registry namespaces="tuxistant-index" ssh-ed25519 AAAAC3Nza...
```

A new registry key ships in a Tuxistant release, and for one release both lines may be shipped. The moderation key is named by the index rather than shipped, so the owner can replace it with a signed index if it leaks. Revocations it already signed stay, which is safe as they only switch things off, and the owner can lift any that were wrong.

## How Tux checks

**The index.** Tux fetches `index.json` and `index.json.sig` and accepts the index only if all of these hold. Otherwise it keeps the one it had, and Settings says why.

1. The signature verifies for `tuxistant-registry` in namespace `tuxistant-index` with the shipped key.
2. `format` is 1 and every field follows this page.
3. `seq` is higher than the one Tux holds, or equal with the same SHA-256 of the bytes. A lower `seq` would hide newer revocations.
4. `date` is no more than a day ahead of the clock.

**Revocations.** Tux fetches `revocations.json` and its signature and takes revocations from it only if:

1. The signature verifies for `tuxistant-moderation` in namespace `tuxistant-revocations` with one of the accepted index's `moderation_keys`, each written as its own `allowed_signers` line.
2. It has exactly the four fields above, `format` is 1, and every entry is a valid revocation entry. Anything else refuses the whole file.
3. `seq` is higher than the one Tux holds for this file, or equal with the same bytes.

**When a revocation is in effect.** Tux keeps every valid revocation entry it has accepted, from either file, by id. The first copy of an id it accepted stands, and a later copy with the same id but different content is ignored, so an entry can't be narrowed once seen. An entry that a newer file leaves out stays. A revocation is in effect until an accepted index lists its id in `lifted`, and a lifted id stays lifted. So the moderation key can only add revocations: it can't lift one, change one, or touch anything else, and only the registry key can undo it.

**A recipe.** To install or update a version, Tux takes its entry from the accepted index and installs it only if:

1. No revocation in effect covers it.
2. `recipes/<sha256>.yaml` has exactly `size` bytes and that SHA-256.
3. Its `key` is in `authors.<author>.keys`, and `versions[].sig` verifies over the bytes for `io.github.<author>` in namespace `tuxistant-recipe`, against a one-line `allowed_signers` with that key and no dates. A signature carries no time, and the registry checked the key's dates when the version was merged.
4. Tux's loader accepts it, and the id, version and permissions it loads match the entry, and `risk_of()` gives the entry's `risk`.

## Example

The published files are in [examples/registry/](examples/registry/), signed, with `registry_allowed_signers` holding the example registry key. Two skills, one with two versions, and one revocation suspending the older version. Shown here with the signatures cut short:

```json
{
  "format": 1,
  "seq": 4,
  "date": "2026-11-20T09:00:00Z",
  "commit": "3f1c2a9e8b7d6c5f4e3d2c1b0a9f8e7d6c5b4a39",
  "moderation_keys": ["ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIJol8M3JliP8HgE4ny4d+l4zrC+8fiIB/opY+2hX6XeB"],
  "categories": [
    {"id": "home", "name": "Home", "tags": ["smart-home", "lights", "heating"]},
    {"id": "information", "name": "Information", "tags": ["weather", "prices", "transport"]},
    {"id": "desktop", "name": "Desktop", "tags": ["settings", "apps"]}
  ],
  "authors": {
    "example-ada": {"status": "active", "keys": [
      {"key": "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAII+YPY+EjRujOtCtSryIuMkrxpewSo80g/E4L9SAcPf1",
       "fingerprint": "SHA256:s82QHFB7Fj/KYRg/hFcxULFv7YdAMtYnJ5jvFVnetWY",
       "added": "2026-11-14", "removed": null}]},
    "example-bo": {"status": "active", "keys": [
      {"key": "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIN7Xa91DaBeBbI5QHwuLTCplGaEm4gprsqCmrv/M066m",
       "fingerprint": "SHA256:vjQBqXKHMz92+gq+RC3g3+y+RkAxGAi5kL5+oNWC5ZM",
       "added": "2026-11-15", "removed": null}]}
  },
  "skills": [
    {"id": "io.github.example-ada.tides", "author": "example-ada", "name": "Tides",
     "summary": "Says when the next high tide is at a seaside town",
     "tags": ["weather", "transport"], "licence": "MIT",
     "links": [{"title": "Source", "url": "https://github.com/example-ada/tides"}],
     "versions": [
       {"version": "1.0.0", "date": "2026-11-14T10:12:00Z",
        "sha256": "570d956fb6109bd32c73d826bbb65b988a8991a15f2b712620440bbf0f8f5497", "size": 495,
        "key": "SHA256:s82QHFB7Fj/KYRg/hFcxULFv7YdAMtYnJ5jvFVnetWY",
        "sig": "-----BEGIN SSH SIGNATURE-----\nU1NIU0lH...\n-----END SSH SIGNATURE-----\n",
        "permissions": {"network": ["api.tides.example"]}, "risk": "medium"},
       {"version": "1.1.0", "date": "2026-11-20T08:41:00Z",
        "sha256": "b76ea8d1d7f493e432cc673e2bfbe70c3198bdf74b0e83f4319e031e20dc449e", "size": 495,
        "key": "SHA256:s82QHFB7Fj/KYRg/hFcxULFv7YdAMtYnJ5jvFVnetWY",
        "sig": "-----BEGIN SSH SIGNATURE-----\nU1NIU0lH...\n-----END SSH SIGNATURE-----\n",
        "permissions": {"network": ["tides.example.org"]}, "risk": "medium"}]},
    {"id": "io.github.example-bo.bluetooth", "author": "example-bo", "name": "Bluetooth page",
     "summary": "Opens the Bluetooth page of Settings",
     "tags": ["settings"], "licence": "CC0-1.0", "links": [],
     "versions": [
       {"version": "1.0.0", "date": "2026-11-15T17:30:00Z",
        "sha256": "20d48e3a09c6005c94b9f16cace6de8a1c85e345e68324aad6f0c116443bab99", "size": 278,
        "key": "SHA256:vjQBqXKHMz92+gq+RC3g3+y+RkAxGAi5kL5+oNWC5ZM",
        "sig": "-----BEGIN SSH SIGNATURE-----\nU1NIU0lH...\n-----END SSH SIGNATURE-----\n",
        "permissions": {}, "risk": "low"}]}
  ],
  "revocations": [
    {"id": 1, "kind": "suspend",
     "scope": {"skill": "io.github.example-ada.tides", "versions": ["1.0.0"]},
     "reason": "Its host, api.tides.example, has been taken over by someone else.",
     "date": "2026-11-19T16:05:00Z", "actor": "example-mod",
     "report": "https://github.com/example/tuxistant-skills/issues/7"}
  ],
  "lifted": []
}
```

The same revocation as it sits in the repo, `moderation/revocations/1.yaml`:

```yaml
id: 1
kind: suspend
scope: { skill: io.github.example-ada.tides, versions: ["1.0.0"] }
reason: Its host, api.tides.example, has been taken over by someone else.
date: "2026-11-19T16:05:00Z"
actor: example-mod
report: https://github.com/example/tuxistant-skills/issues/7
```

## Not decided yet

- Freshness. Someone who controls the host can keep serving the last `revocations.json` and index Tux accepted, and Tux can't tell. A daily re-signed `revocations.json` with an expiry date would let Settings say "I couldn't check for revoked skills this week".
- Publisher key pinning. If the registry key leaked, a forged index could name a new key for a publisher. Tux could ask again, with a card, before an update signed by a key it hasn't seen for that publisher.
- How a publisher withdraws a skill without a moderation action.
