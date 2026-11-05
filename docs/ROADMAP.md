# Roadmap

Where the project stands and what comes next. Keep this current: it is how the next person picks up the work.

Last updated 2026-11-05.

## Where things stand

Steps 1 to 3 of the plan below are built, and most of step 4. Steps 2 to 4 have passed their tests and live tests against the real server, but they have **not yet run on the real desktop**, because the new extension only loads at the next login.

| Part | State |
|---|---|
| Daemon, blob, captions, Fn hold, status icon, Settings, themes | Working on the real desktop |
| Time, disk, weather, news | Working. Weather and news not yet tried by voice |
| Approval gate and card | Tested in an isolated real GNOME Shell. Not yet on the real desktop |
| Files, phone, shell, system facts, translation | Tested with the real model. See "Check on the real desktop" |
| Dictation | Tested in an isolated real GNOME Shell with real windows, and against the real server. Not yet on the real desktop, with a real microphone, or in a real Electron app |
| Facts, web search, research | Tested against the real server, Wikipedia and SearXNG. Not yet by voice on the real desktop |
| Multi-step planner, local model | Two live runs worked |
| Multi-step planner, cloud | Tested against a stub only. Never called the real Claude API |
| Website | On GitHub Pages. See [website/README.md](../website/README.md) |
| Local skill install, code skill sandbox, community registry | Not started |

The code is public under GPL-3.0.

## The plan

The owner approved this order.

1. **Done:** daemon, skill runtime, blob, status icon, Settings.
2. **Done, pending real-desktop checks:** typed skills, then the shell gate, with translation as the first real skill, and the planner with an opt-in cloud model.
3. **Done, pending real-desktop checks: dictation.** Tap Fn, then hold and speak, and the words are typed where the cursor is. No model is involved, so it needs no card. See "Dictation" in [CONTRACT.md](CONTRACT.md).
4. **More built-in skills.** Done, pending real-desktop checks: general web search, research, and facts answered from Wikipedia. **Next:** calendar and journal.
5. **Local install of your own skills.** Recipe skills: YAML only, using built-in daemon actions (D-Bus, HTTP, open app), with no code of their own. They load from a user folder. Non-built-in skills must be refused `shell`.
6. **Code skill sandbox.** A skill with its own code runs as an MCP server in a bubblewrap sandbox with only the access it declared. Local only at first, with a clear warning.
7. **Community registry.** A reviewed Git repo first, like Flathub: skills arrive as pull requests, and a static index is built from them onto the same GitHub Pages site. Signing means the host doesn't have to be trusted, so no server is needed. Logins, ratings or uploads would need one, as a small API beside the site rather than instead of it.

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
- **The site's macOS examples keep naming AirDrop, Spotlight and iPhone.** Tuxistant is meant to do those things on a Mac, so they stay, tagged Soon.

## Waiting on the owner

Don't act on these until they answer.

1. **Custom domain.** A domain on GitHub Pages needs a `CNAME` file and DNS records.
2. **Keeping the connection open about 2 minutes after each Fn hold**, so follow-ups work. Without it each hold is a new conversation and the model forgets the last one. Only `file.find` results carry over, for 5 minutes.
3. **The Scanner theme's voice-lit bar**: keep it or not.
4. **Adding weather and news to the website.**
5. **What "journal" means**: a spoken diary saved as dated files, notes on calendar days, or a particular app. Calendar can use Evolution Data Server, so it follows the accounts in GNOME Online Accounts, and any change needs a card.

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
10. Hold Fn and ask a fact, such as "How many chemical elements are there?". It should answer in about 2 seconds in the usual voice, and History should show `knowledge.answer`. Then something made up, such as "What is aubrium?", which should say it doesn't know.
11. Hold Fn and ask for something that isn't a fact, such as "What time is it?" and "Find my CV". Replies now come from words rather than audio, which has only run in tests and against the real server without a real microphone.
12. "Research electric bikes" says "Let me look into that", then reads the answer with its sources. After reading a file in the same conversation, a search or a fact shows a card first.
13. Dictation, with `tuxistant doctor` saying it's ready: tap Fn, then hold and speak, in a GTK app such as Text Editor, in a terminal, and in VS Code or Chromium, which should take the paste route. Check that the clipboard is unchanged afterwards, that a password box refuses, and that Ctrl+Super+D starts and stops it.

Move site claims from Soon to Now only after the matching check passes.

## Known limits

**The 4B model**, measured with `tools/eval_routing.py`:

- "Update my system" and "Make a folder called Invoices" don't reach the shell. It says it can't, or picks the wrong tool.
- It sometimes opens a file on the way to sending it, which is harmless but not asked for.
- After a declined card it may say "I can't" rather than "OK, I've left that alone".
- It chains `file.find` then `phone.send` itself and rarely picks `task.run`.
- Its answers about the weather can pair days oddly, and "do I need a coat" varies.
- With its tools in front of it, it refuses some plain facts and makes up others, such as "aubrium" for the rarest element. Telling it to look things up doesn't work: it picks the tool about one time in seven, and stronger wording broke "Where's my phone?". That's why hold turns are sorted before it hears them. A turn from the shortcut isn't, and still gets this model. See "Facts" below.

**Facts:**

- Only hold turns are sorted. A turn from the shortcut goes straight to the voice model, as the server replies by itself, so those can still be made up.
- The sorter runs on every hold turn, which adds about 0.4 s to every request, and a stall can add up to 2 s.
- A question goes to Wikipedia with no click, on the word of a 4B model. It called no private request a fact in testing, but it could, and then the words would leave.
- Web search answers from snippets of other people's text, so a false page can make Tux say something false, out loud. It can't do anything.
- It says what pages say, and doesn't know what's fiction. "The capital of Zubrowka" gets Lutz from a fan wiki, with no mention that it's from a film. "Aubrium" gets an Instagram tool of that name.
- Wikipedia is the English one. A question in another language may find less.
- Follow-up questions aren't understood: each hold is a new conversation, and "Who wrote it?" means nothing to a search.
- The voice model never sees an answer the app read out, so it can't take questions about it.

**Web search:**

- A page address can't reach your network or your servers, but it can reach this computer's own public address, which it could only use to send a blind request.
- SearXNG depends on search engines that block automated use. On this server DuckDuckGo, Google and Qwant refuse it, and only Brave and Bing answer. A block can last for hours.

**Dictation:**

- The paste route has only run against GTK with its input method turned off. Real Electron and XWayland apps are untested.
- A password box can only be recognised if it says so through an input method. `Gtk.Entry(visibility=False)` doesn't, and neither can an app with no input method, so words go in like anywhere else. The README tells people not to dictate passwords.
- With no text field focused in an app with no input method, the paste does nothing and the words are gone, with no message. The shell can't tell that from an Electron field.
- The clipboard goes back after a fixed 2 s, as the shell can't see an app reading it. An app slower than that would paste the old clipboard instead of the words. A custom `Meta.SelectionSource` could see the read, and wasn't tried.
- Only one type of clipboard content goes back: text if there was any, else the first type offered. Rich text comes back plain.
- A paste makes the shell the clipboard's owner. A password manager that sets `x-kde-passwordManagerHint` is cleared rather than kept, and one that doesn't loses its auto-clear.
- `Main.inputMethod.commit` goes past any IBus preedit, where the shell's own keyboard goes through the input method. Untested, as the test shell has no IBus.
- A double tap stops a running turn or card, as a Fn hold does. A card stopped this way is a no.
- With a screen that locks on idle, a dictation longer than the idle delay is dropped, as anything is at a lock.
- A process running as you can still own `org.tuxistant.Daemon` while the daemon is down and be believed by the extension, or point `server` elsewhere with `SetConfig`. Either way it chooses the words, though they still can't press Enter.
- Whisper picks the language by itself, and a very short phrase can land in the wrong one. There is no setting yet.
- There is no live text while speaking, no clean-up of "um", and no spoken commands such as "new line".
- If the server can't be reached, the words are lost and it says so.

**A hostile model** (see "A hostile model" in [SECURITY.md](SECURITY.md)):

- Tuxistant can't tell which model the server runs or whether its files have changed. Nothing pins or checks model files, so that is left to whoever runs the server.
- Code running on the server is beyond the reach of the cards. It sees speech, transcripts and what no-click reads return, and it can send any tool call.
- A card can't stop the model urging you to approve it, or lying in a plain answer.
- This has never been tried with a real backdoored model or a hostile model file.

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
