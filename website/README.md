# Website

A short static frontpage: the real theme shaders filling the hero behind the title, taking turns until the visitor picks one, what it can do, and a download button that follows the visitor's operating system. There are static pages per language, with Auto, Light and Dark modes.

## Build and view

```sh
cd website
python3 build.py                                  # writes dist/
cd dist && python3 -m http.server 8000 --bind 127.0.0.1
```

Then open http://127.0.0.1:8000. `build.py` deletes and recreates `dist/`, and a running server keeps working.

### A local preview of planned releases

```sh
python3 website/build.py --preview website/preview/upcoming.json    # writes website/dist-preview/
cd website/dist-preview && python3 -m http.server 8001 --bind 127.0.0.1
```

This shows the site as it will look once the releases in `preview/upcoming.json` ship: they go on top of the releases list, marked Planned with their dates, the preview link names the newest, and their claims join the preview section's (`previewCaps`), all still Soon. Every page has a banner saying it is a local preview. It is never deployed: git ignores `dist-preview/`, and `deploy.sh` builds without `--preview` and refuses a `dist/` holding the preview's `LOCAL-PREVIEW` file. When a planned release ships, move its entry into `src/releases.json` and its claims into the language files, and take them out of `upcoming.json`.

## The skills portal

`skills/` is the registry's portal (step 3 of [docs/REGISTRY.md](../docs/REGISTRY.md)): browse by category and tag, search, and a page per skill with its risk and why, what each action does and whether it asks first, the description the model sees, install steps, the whole `recipe.yaml`, its versions with what changed, and a Report link. `portal.py` builds it from a folder laid out as the registry publishes it (`index.json`, `revocations.json`, `recipes/<sha256>.yaml`; see [docs/REGISTRY-FORMAT.md](../docs/REGISTRY-FORMAT.md)).

**It stays out of the live site until the registry repo exists**, which is the owner's call. The plain build, the one `deploy.sh` runs, reads no registry and writes no portal page: `dist/` has no `skills/` and the front pages have no Skills link. Only `--portal` builds it, into `dist-portal/`, which git ignores, with a `LOCAL-PORTAL` file. `deploy.sh` refuses a `dist/` holding that file or `skills/`, as it refuses the preview's. To see it:

```sh
python3 website/build.py --portal                  # from the sample, into website/dist-portal/
cd website/dist-portal && python3 -m http.server 8002 --bind 127.0.0.1
```

Then open http://127.0.0.1:8002/skills/, or the English front page, whose header links to it in this build. `--portal /path/to/published` builds it from a real registry's files instead. Once the registry repo exists and the owner says so, going live means building the portal in the plain build and taking that refusal out of `deploy.sh`.

Its pages are the front page's: the same header, which hides on the way down, the same colours, type, dark mode and buttons, and a skill's sections fold as the front page's do, What it does open first and Install ending with the download. What is the portal's own is in `src/skills.css` and `src/skills.js`, which only `--portal` copies. The header's rules for the Skills link, and for the icons it and the language menu become on a phone, are in `src/style.css` with the rest of the header, so the plain build carries them unused.

Until then it uses `src/registry-sample/`, made-up skills whose `SAMPLE` file makes every portal page say so and ask not to be indexed. The portal offers them no downloads, install commands, Install button or Report button, as there is nothing to install or report, and shows their publishers, links and reports as words, not links, as those github.com accounts are made up and anybody could register one.

- **It takes only what Tux would, with Tux's own code.** `tuxistant/registry.py` checks the signatures of `index.json` and `revocations.json` and every field of both, finds the revocations in effect, and checks each version that isn't revoked as Browse does before installing it, signature included. The index must be signed by the registry key Tux ships, or, for `src/registry-sample/` itself and no other folder, by the sample's own throwaway key in its `registry_allowed_signers`, which Tux never ships: the files being checked never pick their own key, `SAMPLE` file or not. The build stops if anything fails, or if a recipe isn't the size and hash the index gives, so the portal can't show what Tux would refuse.
- **Everything in the registry is a stranger's text.** Strings are escaped in `portal.py`; hidden characters in a recipe show as their code, and one YAML reads as a line break (`\r`, U+0085, U+2028, U+2029) breaks the line after its code, in the whole recipe and in the diff, so the page shows the lines Tux's loader reads; `skills.js` only hides and shows cards the build wrote.
- **What it does is said from what Tux's loader made of the recipe**, never from the YAML: each action's address, sentence and params, with their defaults as Tux fills them in (a number held to its range, text cleaned and cut). A version's "What changed" starts with what Tux's update card would name, by Browse's own rules (`browse.asks_for`), or says the update needs no card, then names every action that does something else under the same name.
- **Recipes go through Tux's own loader**, so the portal build needs what the daemon imports (PyYAML, websockets and dbus-fast, as `install.sh` lists them) and `ssh-keygen`; the plain build needs none of them. A skill whose current file the loader refuses has no card in Browse, and its page offers nothing to install and makes no claim about its hosts or risk. `skills/recipes/` holds no such file, nor a revoked one, so once the registry pulls a version the next build stops serving it. Hosts in the index are held to the loader's form either way.
- **Any word may break.** A stranger's 140 letters with no space would otherwise run off a phone's screen, so `.portal` sets `overflow-wrap: anywhere`. Headings still never wrap and shrink instead, but a skill's name, up to 100 characters, stops shrinking at 14 px and is cut with an ellipsis, so `site.js` marks a heading it cut and the name is said again in full under it (and in the heading's `title`). Without script it is said again under every heading.
- **Revoked skills are hidden**, by the index's revocations and any newer ones in `revocations.json`, until the index lifts them. As in Tux, every copy of an id is in effect, so `revocations.json` can't narrow one. A suspended version of a skill that is still shown is listed with its reason.
- **No logos yet**, as Tux shows none: an index with logo entries is taken and they are left out.
- **The sample is signed with throwaway keys**, made for one run and deleted, the registry's and the moderation key's included. After changing it, sign it all again with `python3 website/sample_registry.py`. The tests sign the copies they change the same way.
- The portal is in English only, and its Browse page says so. Only the English front page links to it until it is translated. The registry repo's name and its report form's field ids are guesses until they exist: `REGISTRY_REPO` and `report_url` in `portal.py`.

## Deploy

Only when the owner asks.

```sh
website/deploy.sh
```

This builds the site and publishes it to the `gh-pages` branch, which GitHub Pages serves at https://buengenio.github.io/Tuxistant/. That branch holds only the built site, and each deploy replaces it. Pushing to `main` doesn't deploy anything.

For a custom domain, set `SITE_URL` in `deploy.sh`, have `build.py` write a `CNAME` file into `dist/`, and add the DNS records GitHub asks for.

## How it fits GitHub Pages

- **Links are relative**, so the same pages work under `/Tuxistant/` and at the root of a domain. `tests/test_website.py` fails if anything starts from `/`.
- **Pages can't set response headers**, so the Content Security Policy is a meta tag in each page, with hashes for the inline scripts. It's enforced locally too. An inline script added without its hash breaks at once, which is the point. `frame-ancestors`, `nosniff` and `Permissions-Policy` can't be set this way.

## Changing it

- **Text:** `src/i18n/<language>.json`. Adding a language means adding one file. Every claim is tagged `now` or `soon`, and a claim moves to `now` only once the feature has worked on a real machine. `caps` are the hero's claims and `previewCaps` what was added since the stable release that `previewTitle` names, which is why the title gives a version.
- **Versions and download links:** `src/releases.json`, so a release only edits that file. The main download per system stays on the newest stable release; a null `url` shows "coming soon". `preview` is the newest preview, offered as a secondary link that says it hasn't been tried on a real desktop, or null once a stable release is newer. `history` is the releases list, newest first: version, date, `stable` or `preview`, its release page, and `features`: what it added, most impact first, each shown as a chip with an icon. A feature is an `icon` named in `FEATURE_ICONS` in `build.py`, simple line drawings made for this site, and a `label` in every language, in sentence case and short enough for a chip. The build stops if an entry is out of order, lacks a language, names an unknown icon, has a label that starts with a small letter or runs past 40 characters, which is about one line on a phone, or the preview or main download isn't in it.
- **Works with:** the `WORKS_WITH` list in `build.py`, one line per brand: name, mark, and `now` or `soon`. Marks are files in `src/logos/`, and `src/logos/SOURCES.md` says where each came from and on what terms. A mark of `None` sets the name in text. Add a new name to the `trademarks` line in every language, or the tests fail. The ticker is CSS only, and stands still for people who ask for reduced motion.
- **Sections:** everything after the ticker is an accordion, opened and closed by `acc_open()` and `acc_close()` in `build.py`. Each arrives open with a plain heading, which is how it stays without script; `site.js`, which every page loads, makes the heading's text the toggle button, closes all but the first section and opens the one a link points into. `app.js` is the front page's own: the download, the examples' tabs and the hero. Each ends with a call to action, worded by the `cta*` keys in every language. A section's download names the system it is for, as the hero's does, with or without script. The examples' prompt follows the tab on show: a system with no release yet gets `ctaSoon`, that it is coming, then `ctaSoonAlso`, whether the visitor has a desktop the download beside it runs on. The Donate card below stays the page's one donation ask. A new section needs a prompt and an action, or the tests fail.
- **Themes:** `build.py` takes them from `../themes/`. They run in WebGL1 here and in Cogl on the desktop, so check a shader change in both. Band themes have an ink colour for light mode, in the footer of the shader in `src/app.js`. In the hero (`place()` in `src/app.js`) a band runs edge to edge. A bar, such as the scanner, sits right behind the name: it keeps to the middle column beside the claims and inside the 16 px gutter on a phone, stays above the tagline and below the header, and the name turns light as it fades in and dark as it fades out. Any other theme is centred on the words, with its top peeking over the name. The themes take turns until the visitor picks one, and hold their turn while the pointer or the keyboard is on the picker. A button focused by a click or a tap doesn't hold it. `tests/test_website.py` checks this by running `app.js` in node on a small fake page (`tests/website_hero_page.mjs`), with a clock it moves by hand. The pause button beside the dots, or reduced motion, draws one still frame: Tux listening, with his eyes open. A new theme also needs a still CSS drawing in `src/style.css` for browsers without WebGL, and the tests fail without one; without script nothing places it, so it sits centred, peeking over the name and clear of the claims. Check that the words still read on it, in light and dark. The words over a theme carry no blur, since the canvas redraws every frame: the small lines under the button sit on nearly solid pills, and the claims are nearly solid, so Now and Soon keep 4.5:1 where a band runs behind them on a wide screen.
- **Layout rules:** H1 and H2 never wrap at any width, and gaps stay tight. On a phone the title, the download button and the theme picker fit on the first screen, and the claims follow under them. The tagline breaks between its sentences and into even lines, and on a touch screen the theme dots and the pause button are a thumb wide. On a wide screen the hero's spare height goes above the name, where a figure peeks out, so the ticker follows close under the picker.
- **Header:** the same on every page, from `site_header()` in `build.py`, with its code in `site.js`. It hides on the way down and comes back on the way up. A jump, such as a link that opens a folded section, keeps it showing, even when a busy phone reports the jump's scroll after the grace: the first scroll within 3 s of a jump counts as the jump. Its blur is on the `.nav-glass` layer inside it, never on the header, which moves with `transform`. A blur on a moving element has nothing behind it to blur.
