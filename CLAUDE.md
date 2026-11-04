# Notes for agents working on Tuxistant

Tuxistant is a voice assistant for the Linux desktop: a Python daemon, a GNOME Shell extension, a Settings app, and a LocalAI server for speech and the model. The plan includes skills people can share.

**This repo is public.** Notes about the owner's machine and accounts belong in `CLAUDE.local.md`, which git ignores. If that file exists, read it too.

## Read these first

| File | What it holds |
|---|---|
| [docs/ROADMAP.md](docs/ROADMAP.md) | Where things stand, the approved plan, decisions waiting on the owner, what to check on the real desktop, and the release checklist. **Start here** |
| [docs/CONTRACT.md](docs/CONTRACT.md) | How the parts talk: D-Bus, approvals, the realtime protocol, the Fn helper. Change it here first |
| [docs/SKILLS.md](docs/SKILLS.md) | Manifests, permissions, what needs a click, the shell skill |
| [docs/THEMES.md](docs/THEMES.md) | Writing a theme |
| [docs/TESTING.md](docs/TESTING.md) | The test layers, the live evaluations, and what can go wrong on a real desktop |
| [website/README.md](website/README.md) | Building and deploying the site |

## Rules that are easy to break

- **Commit, push, deploy, change DNS or publish a download only when asked.** The owner decides each one, and a push here is published to the world.
- **Never print, log or store an API key.** The LocalAI key is in `~/.config/tuxistant/config.json` (mode 0600). Load it with `tuxistant.config.Config`. Don't go looking for an Anthropic key.
- **Nothing in a test or evaluation may reach the real desktop, phone, clipboard, Trash or files.** See "Staying off the real desktop" in docs/TESTING.md. This has gone wrong three times.
- **Never restart the real GNOME Shell or try to reload the extension in it.** A running Wayland session can't load changed extension code. Use the isolated harness.
- **Don't send anything to a device or add anything to the owner's calendar** unless asked for that exact thing.
- **Every risky action goes through `Skills.execute` and an `Ask`.** Never add a way round it, including for the planner. Card text comes from daemon code looking at the real action, never from a manifest or the model.
- **Shell access stays built in.** Community skills must never be able to request it.

## How the code is written

- Short functions, and comments only where they explain why.
- Error text is spoken aloud, so write plain sentences.
- The model is a 4B: it ignores wording rules, repeats what it's given word for word, mangles long strings and invents arguments. Put robustness in the handler and the shape of the result, then check with `tools/eval_routing.py` and `tools/eval_flows.py`. Tool descriptions are the one place wording works, as they decide which tool it picks.
- No new dependencies without a reason. The daemon runs on the system Python with `-P`.
- Website: H1 and H2 never wrap, layouts stay tight, and demos fit the viewport.

## Running it here

- `~/.local/bin/tuxistant` runs the code from this folder, so a daemon restart picks up Python changes: `systemctl --user restart tuxistant`.
- The extension is a copy. `./install.sh` updates it, and it loads at the next login. Nothing can be approved until the new one has loaded, and the daemon says so out loud rather than waiting.
- `tuxistant doctor` checks everything, including approval cards. If the desktop misbehaves: `gnome-extensions disable tuxistant@buengenio`.

## Working with the owner

For big build rounds the owner asked for as much parallel work as possible. Keep reports short and plain, say what was really run and what wasn't, and tell them straight away if something touched their desktop.
