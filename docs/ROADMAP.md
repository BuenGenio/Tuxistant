# Roadmap

Where the project stands and what comes next. Keep this current: it is how the next person picks up the work.

Last updated 2026-11-13.

## Where things stand

Steps 1 to 3 of the plan below are built. `v0.2.0` is the latest stable release: it holds only what has worked on the owner's real desktop, plus what 0.1.0 already had. Steps 2 and 3 have passed their tests and live tests against the real server, and parts of them have now run on the real desktop: the list under "Check on the real desktop" says which.

| Part | State |
|---|---|
| Daemon, blob, captions, Fn hold, status icon, Settings, themes | Working on the real desktop |
| Time, disk, weather, news | Working on the real desktop, by voice, with BBC News and The Kyiv Independent as the source |
| Approval gate and card | Working on the real desktop: cards for adding calendar events and running a command were shown and allowed on 2026-11-11. Deny and the lock-screen path not yet tried there |
| Files, phone, shell, system facts | Tested with the real model. See "Check on the real desktop" |
| Translation | Spoken translation works on the real desktop. Translating a selection has not been tried there |
| Calendar | Reads and adds events through Evolution Data Server. Tested against a real, private Evolution Data Server in two time zones, and reading was tried on the owner's ten Google calendars. On 2026-11-11 it read the calendar by voice and added four events through the card, to the local Personal calendar. Adding has not run on an online account, and CalDAV and Microsoft accounts have not been tried at all |
| Tapo lights | Working on the real desktop with an L530. The bulb sometimes went missing until the Tapo app woke it. Since 2026-11-10 bulbs are remembered and asked directly. Over the next day it answered four of five times, and once said it couldn't find any lights |
| Dictation | Tested in an isolated real GNOME Shell with real windows, and against the real server. Not yet on the real desktop, with a real microphone, or in a real Electron app |
| Multi-step planner, local model | Two live runs worked |
| Multi-step planner, cloud | Tested against a stub only. Never called the real Claude API |
| Website | On GitHub Pages. See [website/README.md](../website/README.md) |
| Demo video | A 64 second draft from `tools/demo_video.py`: the real extension in an isolated shell, with scripted requests and synthesised voices, and it says so on screen. The owner accepted it on 2026-11-10. Not published anywhere yet |
| Server kit | In `server/`, copied from the owner's working server with its host name and user id made general. That general copy has not yet set up a second machine |
| Local skill install, code skill sandbox, community registry | Not started |

The code is public under GPL-3.0.

## The plan

The owner approved this order.

1. **Done:** daemon, skill runtime, blob, status icon, Settings.
2. **Done, pending real-desktop checks:** typed skills, then the shell gate, with translation as the first real skill, and the planner with an opt-in cloud model.
3. **Done, pending real-desktop checks: dictation.** Tap Fn, then hold and speak, and the words are typed where the cursor is. No model is involved, so it needs no card. See "Dictation" in [CONTRACT.md](CONTRACT.md).
4. **Under way: more built-in skills.** Calendar is built. General web search, research and journal are next.
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

### Choices made while building, worth a look

- With Cloud on, a conversation that has already read something private gets a "Send this request to Claude?" card before the task starts, because the voice model could paste file text into the goal. Every task sets that state, so a second task in one conversation always shows it. `tuxistant/planner.py`, about 3 lines to remove.
- Cloud uses `claude-opus-5-5` at low effort, because someone is waiting in silence, with server-side refusal fallbacks on. `CLOUD_EFFORT` is one constant.
- The consent dialog shows every time Cloud goes from off to on, not literally once. A single-time prompt needs a new config key.
- `file.open` refuses HTML, SVG, archives and disk images as well as anything that runs code.
- Translations of more than 2000 characters are refused, not cut.

## Check on the real desktop

None of this can be proved from a test. After `./install.sh` and a logout:

1. **Passed 2026-11-10:** `tuxistant doctor` says approval cards are ready, and dictation too.
2. **Passed in part 2026-11-11:** cards appeared and Allow worked, for calendar events and a command. A card appears, Deny and Allow both work, and the blob comes back for a card after an Fn hold was released.
3. **Passed 2026-11-11:** "Open my CV" opens it. A PDF was found and opened by voice, from inside `tuxistant.service`.
4. "Where's my phone?" rings it, and sending a small file arrives. No real transfer or ring has ever been done.
5. "Install VLC" or any `sudo` command shows a password prompt. `pkexec` from a systemd user service has never run for real.
6. "Translate what I've selected into French" reads the selection. A translation spoken in its own voice in a whole voice turn passed on 2026-11-10.
7. Locking the screen during a card cancels it. The logind lock path is untested.
8. **Passed 2026-11-10:** weather and news by voice.
9. Cloud, once the owner adds a key: run `./install.sh --cloud-sdk`, then try one multi-step request.
10. Dictation, with `tuxistant doctor` saying it's ready: tap Fn, then hold and speak, in a GTK app such as Text Editor, in a terminal, and in VS Code or Chromium, which should take the paste route. Check that the clipboard is unchanged afterwards, that a password box refuses, and that Ctrl+Super+D starts and stops it.

11. **Passed 2026-11-10:** lights, with the TP-Link account in Settings: "Is the light on?", then "Turn off the light" and "Make it warm white".
12. **Passed in part 2026-11-11:** read by voice, and four events added through the card to the local Personal calendar. Adding has not yet written to an online account. Calendar: "What's on my calendar tomorrow?" by voice, then "Add a test event on Friday at 3pm", allow the card, and see it appear in GNOME Calendar and on the account. Pick a calendar in Settings first.

Move site claims from Soon to Now only after the matching check passes.

## Known limits

**The 4B model**, measured with `tools/eval_routing.py`:

- "Update my system" and "Make a folder called Invoices" don't reach the shell. It says it can't, or picks the wrong tool.
- It sometimes opens a file on the way to sending it, which is harmless but not asked for.
- After a declined card it may say "I can't" rather than "OK, I've left that alone".
- It chains `file.find` then `phone.send` itself and rarely picks `task.run`.
- Its answers about the weather can pair days oddly, and "do I need a coat" varies.
- It still refuses some plain facts, and always refuses "What's 15 percent of 80?". With no tools in front of it, it answered 66 of 66, so the tool list causes this, not its knowledge. A tool that asks the model again without the tool list might close the gap. Not tried yet.
- Adding the two light tools cost plain facts: 43 of 50 answered without them, 35 to 40 of 50 with them (5 repeats, 2026-11-10). "When did World War Two end?" went from 5 of 5 to 0 of 5. Shorter wording and a single tool didn't help, as the refusals only moved to other questions. All 27 light requests route right.
- About 1 fact in 80 comes back confidently wrong, such as Charlotte Brontë for Pride and Prejudice, and "What language do they speak in Brazil?" can reach the translate tool.

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
- The approval card's wait before Allow works now starts from a painted frame. That fix is in the extension, so the real desktop has it only after `./install.sh` and a login.
- `./uninstall.sh` leaves the extension's name in GNOME's list of enabled extensions. It is harmless, and installing again finds it already enabled.
- Nothing has run on x86-64 or on Fedora. The server kit is x86-64 only and the desktop has only run on aarch64.
- Approval cards are click-only, with no keyboard path. During a popup menu or a polkit dialog a click may not reach the card, which then times out as a no.
- Kokoro is English-only here. Ukrainian uses the lowest-quality Piper voice, as no better one exists in the gallery. Chinese Traditional has not been tried on the Mandarin voice.
- The calendar can read and add, and nothing else: no changing, moving or deleting an event, no invitations, no locations and no alerts. "Remind me to call the dentist tomorrow" becomes an all-day calendar entry, with a card, not an alert.
- The model sometimes leaves out how long an event lasts, so the card offers an hour. The card shows the times before anything is saved.
- Microsoft calendars need the `evolution-ews` package, which adds them to Evolution Data Server. That route is untested, as is any CalDAV server other than Google's.
- A time with no am or pm from 1 to 6 is taken as the afternoon. "At 7" is the morning.
- News comes from a list of nine sources in Settings, all in English, as the voice reads other languages with English sounds. A feed of your own can't be added: the list is fixed so that a news request can only reach hosts named in the code, and a free address would need the permission display and the cards to show a host the user chose.
- Whisper mishears some place names, so a weather request can ask for a town that doesn't exist. It says the town wasn't found.
- A Tapo bulb whose address changes while it also ignores broadcasts stays lost until it answers one. Its old address is tried first, which costs up to 4 seconds.
- A shader trial can't prove a theme safe. Waves is click-through, so its menu is the panel icon's.

## Before any public download

- **Done 2026-11-10:** the owner's defaults are gone. The server starts as `localhost:8080`, and the owner's own `config.json` now names their server. Any other machine of the owner's needs the server set in Settings.
- **Done 2026-11-10:** `server/` sets up the LocalAI side, which the README used to ask for without saying how.
- **Done 2026-11-10:** a rehearsal on a clean Ubuntu 26.04 in a container, following only the README. It found the README named no packages and no way to get the code, the installer said "installed" with modules missing, and `doctor` crashed. All fixed. With two packages added, the install, the extension in Ubuntu's own GNOME Shell 50.1, the daemon, Settings and the uninstall worked. It could not check a real login, the Fn helper running, audio, or any server.
- Run `server/setup.sh` on a machine that isn't the owner's.
- When there is a deb or an rpm: the daemon's Python modules are dependencies, and the optional skills' needs are recommendations, so they come by default and can be left out. For the calendar that is `evolution-data-server`, `gir1.2-ecal-2.0` and `gir1.2-edataserver-1.2` on Debian and Ubuntu, and `evolution-data-server` alone on Fedora, which ships the bindings inside it. For lights it is `python3-cryptography`, and for the sandbox `bubblewrap`.
- Give the default theme a shape and colours of its own.
- **Done 2026-11-10, after 0.1.0:** the news source is a choice in Settings.
- **Done 2026-11-10:** `v0.1.0` is tagged as a preview, and the site's download button points at the tarball GitHub builds for the tag. [CHANGELOG.md](../CHANGELOG.md) has the text of the release page.
- **Done 2026-11-13:** `v0.2.0` is tagged as the first stable release, from branch `stable-0.2`: 0.1.0 with the calendar, the choice of news source, and tests that pass on a fresh clone. Only what had worked on the real desktop went in. The rest is for 0.3.
- Have native speakers check the site's translations, including the Auto/Light/Dark words. Five languages now, 25 to 30 later.
- Replace the favicon, which is still the old penguin.
