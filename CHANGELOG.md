# Changelog

## Not yet released

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
