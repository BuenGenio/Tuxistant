# Roadmap

Where the project stands and what comes next. Keep this current: it is how the next person picks up the work.

Last updated 2026-11-04.

## Where things stand

Steps 1 and 2 of the plan below are built. Step 2 has passed its tests, a security review and live tests against the real model, but it has **not yet run on the real desktop**, because the new extension only loads at the next login.

| Part | State |
|---|---|
| Daemon, blob, captions, Fn hold, status icon, Settings, themes | Working on the real desktop |
| Time, disk, weather, news | Working. Weather and news not yet tried by voice |
| Approval gate and card | Tested in an isolated real GNOME Shell. Not yet on the real desktop |
| Files, phone, shell, system facts, translation | Tested with the real model. See "Check on the real desktop" |
| Multi-step planner, local model | Two live runs worked |
| Multi-step planner, cloud | Tested against a stub only. Never called the real Claude API |
| Website | On GitHub Pages. See [website/README.md](../website/README.md) |
| Local skill install, code skill sandbox, community registry | Not started |

The code is public under GPL-3.0.

## The plan

The owner approved this order.

1. **Done:** daemon, skill runtime, blob, status icon, Settings.
2. **Done, pending real-desktop checks:** typed skills, then the shell gate, with translation as the first real skill, and the planner with an opt-in cloud model.
3. **Next: local install of your own skills.** Recipe skills: YAML only, using built-in daemon actions (D-Bus, HTTP, open app), with no code of their own. They load from a user folder. Non-built-in skills must be refused `shell`.
4. **Code skill sandbox.** A skill with its own code runs as an MCP server in a bubblewrap sandbox with only the access it declared. Local only at first, with a clear warning.
5. **Community registry.** A reviewed Git repo first, like Flathub: skills arrive as pull requests, and a static index is built from them onto the same GitHub Pages site. Signing means the host doesn't have to be trusted, so no server is needed. Logins, ratings or uploads would need one, as a small API beside the site rather than instead of it.

### Rules for shared skills

The owner is specifically worried about malicious skills. Assume a bad skill will eventually get past review, so permissions and the sandbox are the real protection.

- Risk is computed from permissions, never taken from the manifest.
- Sharing is recipe-only at first, because a recipe can be checked automatically.
- Versions are pinned by hash, and any new permission needs approving again, so a harmless v1 can't turn malicious in v2.
- Publishers sign their skills and the daemon verifies them, so the site itself doesn't need to be trusted. Publisher namespaces stop lookalike names.
- Skill descriptions and results are untrusted data, and only a short, limited description reaches the model.
- The shell skill is never shareable and can't be called by a community skill.

## Decisions already made

These were agreed with the owner, so don't reopen them without a reason.

- **Everything is a skill**, including the built-ins.
- **Approval is a click, never a voice "yes"**, because audio playing nearby could say it.
- **After private data is read, nothing leaves your own server without a click**, whichever skill asks.
- **Cloud is off by default, with a one-time opt-in.** Claude sees the request plus file names and command output from that task, and file contents only if you approve each one. It uses the owner's own API key.
- **Deleting goes to the Trash** wherever possible, and previews first.
- **`sudo` always gets its own approval.**
- **Themes are data**: a JSON file and a GLSL shader, and the website runs the same shaders.
- **The site shows Now or Soon on every claim.** A feature moves to Now only once it has worked on a real machine.

## Waiting on the owner

Don't act on these until they answer.

1. **Custom domain.** A domain on GitHub Pages needs a `CNAME` file and DNS records.
2. **Web search skill.** Self-hosted SearXNG on stormshadow suggested.
3. **Keeping the connection open about 2 minutes after each Fn hold**, so follow-ups work. Without it each hold is a new conversation and the model forgets the last one. Only `file.find` results carry over, for 5 minutes.
4. **The Scanner theme's voice-lit bar**: keep it or not.
5. **Adding weather and news to the website.**

### Choices made while building, worth a look

- With Cloud on, a conversation that has already read something private gets a "Send this request to Claude?" card before the task starts, because the voice model could paste file text into the goal. Every task sets that state, so a second task in one conversation always shows it. `tuxistant/planner.py`, about 3 lines to remove.
- Cloud uses `claude-opus-5-5` at low effort, because someone is waiting in silence, with server-side refusal fallbacks on. `CLOUD_EFFORT` is one constant.
- The consent dialog shows every time Cloud goes from off to on, not literally once. A single-time prompt needs a new config key.
- `file.open` refuses HTML, SVG, archives and disk images as well as anything that runs code.
- Translations of more than 2000 characters are refused, not cut.

## Check on the real desktop

None of this can be proved from a test. After `./install.sh` and a logout:

1. `tuxistant doctor` says approval cards are ready.
2. A card appears, Deny and Allow both work, and the blob comes back for a card after an Fn hold was released.
3. "Open my CV" opens it. `gio open` has only ever run from a test, not from inside `tuxistant.service`.
4. "Where's my phone?" rings it, and sending a small file arrives. No real transfer or ring has ever been done.
5. "Install VLC" or any `sudo` command shows a password prompt. `pkexec` from a systemd user service has never run for real.
6. "Translate what I've selected into French" reads the selection, and a translation is spoken in its own voice in a whole voice turn.
7. Locking the screen during a card cancels it. The logind lock path is untested.
8. Weather and news by voice.
9. Cloud, once the owner adds a key: run `./install.sh --cloud-sdk`, then try one multi-step request.

Move site claims from Soon to Now only after the matching check passes.

## Known limits

**The 4B model**, measured with `tools/eval_routing.py`:

- "Update my system" and "Make a folder called Invoices" don't reach the shell. It says it can't, or picks the wrong tool.
- It sometimes opens a file on the way to sending it, which is harmless but not asked for.
- After a declined card it may say "I can't" rather than "OK, I've left that alone".
- It chains `file.find` then `phone.send` itself and rarely picks `task.run`.
- Its answers about the weather can pair days oddly, and "do I need a coat" varies.

**Elsewhere:**

- `file.find` ranking is weak for words common in unpacked archives, such as "tax return", and `JUNK_FOLDERS` was partly shaped by this machine.
- A folder scan stops after 12 seconds, so on a big home folder the order is approximate and the answer says so.
- Commands that need the real network or process list run with a click, because the sandbox hides both. Common questions are covered by `system.info` and `system.running`.
- In the sandbox hidden folders look empty, so `du ~` and `find ~` undercount with no warning. `disk.folders` counts them properly. Naming a hidden folder gets a card.
- Sandboxed `git status` loses what lives in `.git/config`, such as the upstream line, and dirty submodules don't show.
- The secret scan for non-hidden folders stops at depth 4, and new files wait up to 5 minutes for the next scan. A `.env` deeper than that is readable, though flagged as file contents. Folders the scan can't reach are hidden.
- Thousands of secret-named files directly in the home folder make every sandboxed command fail. It fails shut, but it does stop the sandbox.
- The seccomp filter has only run for real on aarch64. On x86_64 it was checked with a BPF interpreter.
- A process already running as you, outside the sandbox, can do whatever you can. Only the shell can answer a card, but card text is broadcast on the session bus.
- Approval cards are click-only, with no keyboard path. During a popup menu or a polkit dialog a click may not reach the card, which then times out as a no.
- Kokoro is English-only here. Ukrainian uses the lowest-quality Piper voice, as no better one exists in the gallery. Chinese Traditional has not been tried on the Mandarin voice.
- BBC news is UK-centric and its source isn't configurable.
- A shader trial can't prove a theme safe. Waves is click-through, so its menu is the panel icon's.

## Before any public download

- Remove the owner's defaults: `stormshadow` in `tuxistant/config.py` and the `stormshadow` examples in `tuxistant/settings_app.py`. Move any saved settings along with it, because `config.json` only stores what differs from the defaults.
- Give the default theme a shape and colours of its own.
- Make the news source configurable.
- Build a tarball, set `url` in `website/src/releases.json`, then run `website/deploy.sh`.
- Have native speakers check the site's translations, including the Auto/Light/Dark words. Five languages now, 25 to 30 later.
- Replace the favicon, which is still the old penguin.
