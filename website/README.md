# Website

A short static frontpage: Tux in the hero rotating through the real theme shaders, what it can do, and a download button that follows the visitor's operating system. There are static pages per language, with Auto, Light and Dark modes.

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

This shows the site as it will look once the releases in `preview/upcoming.json` ship: they go on top of the releases list, marked Planned with their dates, the preview link names the newest, and their claims join "New in the preview", all still Soon. Every page has a banner saying it is a local preview. It is never deployed: git ignores `dist-preview/`, and `deploy.sh` builds without `--preview` and refuses a `dist/` holding the preview's `LOCAL-PREVIEW` file. When a planned release ships, move its entry into `src/releases.json` and its claims into the language files, and take them out of `upcoming.json`.

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

- **Text:** `src/i18n/<language>.json`. Adding a language means adding one file. Every claim is tagged `now` or `soon`, and a claim moves to `now` only once the feature has worked on a real machine. `caps` are the hero's claims and `previewCaps` what the previews added.
- **Versions and download links:** `src/releases.json`, so a release only edits that file. The main download per system stays on the newest stable release; a null `url` shows "coming soon". `preview` is the newest preview, offered as a secondary link that says it hasn't been tried on a real desktop, or null once a stable release is newer. `history` is the releases list, newest first: version, date, `stable` or `preview`, its release page, and one line of what it added in every language. The build stops if an entry is out of order, lacks a language, or the preview or main download isn't in it.
- **Works with:** the `WORKS_WITH` list in `build.py`, one line per brand: name, mark, and `now` or `soon`. Marks are files in `src/logos/`, and `src/logos/SOURCES.md` says where each came from and on what terms. A mark of `None` sets the name in text. Add a new name to the `trademarks` line in every language, or the tests fail. The ticker is CSS only, and stands still for people who ask for reduced motion.
- **Themes:** `build.py` takes them from `../themes/`. They run in WebGL1 here and in Cogl on the desktop, so check a shader change in both. Band themes have an ink colour for light mode, in the footer of the shader in `src/app.js`.
- **Layout rules:** H1 and H2 never wrap at any width, gaps stay tight, and the stage fits the viewport on a phone.
