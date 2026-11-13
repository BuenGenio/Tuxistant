# Changelog

## Not yet released

- **Your own skills, with code.** A skill can bring its own program, which runs in a bubblewrap sandbox and may ask for the network and nothing else. Put one in `~/.local/share/tuxistant/skills/` and it loads, changes and goes while Tux runs, with no restart. [docs/SKILLS.md](docs/SKILLS.md#code-skills) shows how to write one. Tapo lights now run this way, and find their bulbs again with a broadcast the first time after updating.
- **Home Assistant.** "Turn off the kitchen light", "Is the front door locked?" and "Set the heating to 21". It controls and reads lights, plugs, fans, heating, blinds, locks, scenes and scripts by the names they have in Home Assistant. Unlocking, opening, activating a scene and running a script show a card first. Add the address and a long-lived access token in Settings. Until both are set the model isn't offered it, so nothing changes for anyone without one. The token is never handed back, and a new address clears it.
- **Timers and reminders.** "Set a pasta timer for 8 minutes", "Remind me at 5pm to call mum" and "How long is left on the pasta?". Tux says it out loud when it is due and leaves a notification, and they are kept through a restart. On a locked screen there is only the notification, and the words come if you unlock within an hour for a timer, or a day for a reminder. After that only the notification remains. "Chime for timers" in Settings, which starts off, adds a short sound. The daemon now starts at login on every install, with or without the Fn helper, as a reminder can't be said if nothing is running.
- **Media.** "Pause the music", "Skip this song", "What's playing?", "Turn it up" and "Volume 40". It works with Spotify, VLC, browsers and anything else that speaks MPRIS, and the volume never goes above 100 percent.
- **Mail search.** "When did I book the flights?" or "What did my landlord say about the rent?" is answered from the mail Thunderbird keeps on this computer, with who sent it and when. It is off until you turn on "Search my mail" in Settings. Mail goes nowhere but your own server. Junk and Trash are never read, sign-in codes and passwords are hidden from the rest, and an answer from a sender who is new to you comes with a warning.
- **Facts from what's written down.** A question of general knowledge asked on an Fn hold is answered from Wikipedia, then from web search, and Tux says so when neither clearly answers, instead of making something up. Sums are worked out on this computer. "Look up facts on Wikipedia" in Settings turns the lookup off.
- **Web search and research.** "Search the web for flights to Lisbon" reads out the top results, and "Research electric bikes" reads a few pages and tells you what they say, naming its sources. Both need a SearXNG server, which Settings can point at.

## 0.2.0

What has been added since 0.1.0 and has worked on the owner's own desktop, not only in tests. The calendar, news and file code here is the code that ran there, inside a development build whose turn handling was newer. Work that is built but not yet proved that way, such as web search, mail search, media control, timers, Home Assistant and skills with their own code, comes in 0.3.

Some of what 0.1.0 brought has still only run in tests and in an isolated GNOME Shell, such as dictation, ringing a phone and `sudo` commands. [docs/ROADMAP.md](docs/ROADMAP.md) lists them under "Check on the real desktop". It needs the same as 0.1.0: GNOME Shell 50 on Wayland, Python 3.14, and a LocalAI server of your own with an 8 GB NVIDIA GPU.

- **Calendar.** "What's on my calendar tomorrow?", "When's my next meeting?" and "Add dentist on Friday at 3pm". It works with any calendar that shows in GNOME Calendar, so a Google, CalDAV or Microsoft account is added once in GNOME Settings under Online Accounts and Tuxistant never holds its password. Adding shows a card with the title, date, times and calendar first. Settings chooses which calendar new events go to. Reading has been tried on Google calendars and adding on a local one. CalDAV and Microsoft accounts take the same path but have not been tried.
- **News from a source you choose.** Settings has a list of nine: BBC News, The Guardian, NPR, CBC News, ABC News Australia, DW, France 24, Al Jazeera and The Kyiv Independent. A topic your source doesn't cover gets its top stories, and Tux says so. A redirect to any other host is refused.
- **A fresh clone's tests pass.** The test harnesses now compile the extension's settings schema themselves. Before, about 137 tests failed on a new checkout because the compiled schema isn't kept in git.

## 0.1.0, a preview

The first tagged release. It is a preview: everything here is built and tested, but some of it has only run in tests and in an isolated GNOME Shell, not yet on a desktop in daily use. [docs/ROADMAP.md](docs/ROADMAP.md) says which parts those are, under "Where things stand".

It needs GNOME Shell 50 on Wayland, Python 3.14, and a LocalAI server of your own with an 8 GB NVIDIA GPU. [README.md](README.md) has the full list and the install steps, and [server/](server/README.md) sets up the server.

What it does:

- **Talk and listen.** Press `Ctrl+Super+Space` or hold Fn, ask, and Tux answers out loud. A blob on screen reacts while captions show what was heard and said. Speech, the model and the voice all run on your own server.
- **Dictation.** Tap Fn, then hold it and speak, and your words are typed where the cursor is. It never presses Enter.
- **Skills.** Weather and news, finding and opening files, sending a file to your phone or ringing it, moving old files to the Trash, Tapo smart lights, translation spoken in a native voice, facts about this computer, and shell commands.
- **Approval cards.** Anything that changes something or sends a file off the computer shows exactly what will happen and waits for a click. Read-only commands run in a sandbox without one.
- **Several steps in one request**, on your own server, or with Claude if you turn on Cloud and add your own key. Cloud is off by default.
- **Four themes**, and you can write your own as a JSON file and a shader.
- **A Settings app** for the server, voice, look, skills and history, and `tuxistant doctor` to check an install.
- **A server kit** in `server/`: one script that installs LocalAI, the models and the voices, then tests them.

Known limits are listed in [docs/ROADMAP.md](docs/ROADMAP.md), and what the security reviews found and didn't cover is in [docs/SECURITY.md](docs/SECURITY.md).
