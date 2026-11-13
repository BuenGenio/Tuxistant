# Changelog

## Not yet released

- Tux opens apps and Settings pages: "Open Firefox", "Open Spotify", "Open the Bluetooth settings", "Open the display settings". It opens only what is installed, and asks which one when several match. A shortcut that isn't from an installed package, such as one added in your home folder, shows a card first with the command it runs, and says when it starts over D-Bus, in a terminal or in another folder. "Open Firefox", "Open Spotify" and the Settings pages reach it every time in the measured runs, and "Close Firefox" no longer opens Firefox. Not yet tried on a real desktop.
- Home Assistant finds a device by any whole word in the first 255 characters of its name, not only the first 40. "Garage door" now finds a switch named "Shelly relay channel one outside wall garage door", and the words of "(9 more characters)" no longer find anything. Areas work the same way. What Tux says and the card still show the name cut to 40 characters.
- The facts lookup now refuses questions about the music playing, timers and the home, such as "Is the front door locked?", on every turn. When the voice model picks it, on a turn from the shortcut or a hold turn not sorted as a fact, the question no longer goes to Wikipedia or web search: Tux says it didn't look it up, or the model uses the right tool. Facts that share their words are still looked up, such as "How many planets are in our solar system?", "Who invented the egg timer?" or a title such as My Way.
- On an Fn hold, questions that only hold now, with words such as today, tomorrow, latest or currently, and questions about Tux itself, such as "What are your skills?" or "How can you help me?", now go the normal way without being sorted. So do questions about the colour or brightness of a lamp, light or bulb, which the facts lookup refuses as well. A title with a capital, such as the song Yesterday, is still sorted.

## 0.3.0, a preview

A preview. Most of what is new here has run only in tests and in evaluations against a real model, not yet on the owner's own desktop. Mail search was asked four times there: two answers, and two turns cut short. Web search, facts, your own skills, media, timers and Home Assistant have not yet been tried by voice on a real desktop, and Home Assistant has never been used with a real home.

Three changes reach what 0.2.0 already did:

- Every Fn hold is now turned into words first, and a separate model call decides whether it is a question of fact, before the voice model hears it. Apart from the four mail questions, which may have gone this way, it has not run on a real desktop.
- Tapo lights, which worked in 0.2.0, now run in the skill sandbox. That needs `bwrap` and `prlimit`, an x86_64 or aarch64 computer, and Linux 6.12 or newer with Landlock on. Lights needed none of this in 0.2.0, so without it they stop working after the update. As a skill with the network, they also now ask for a click when the conversation has already read something private, such as your calendar or a file, which 0.2.0 never did. They have not been tried on a real desktop since the move.
- The daemon now starts at login on every install, not only with the Fn helper. That changes nothing for an install with the Fn helper, and has not been tried without one.

After updating, restart the daemon with `systemctl --user restart tuxistant`, or log out and back in. Until then the old version keeps running. For only what has been seen working, stay on 0.2.0.

How well the model picks the right tool with all of this offered is measured in [docs/TESTING.md](docs/TESTING.md), with what it still gets wrong, such as "Open Spotify" pressing play.

- **Your own skills, with code.** A skill can bring its own program, which runs in a bubblewrap sandbox and may ask for the network and nothing else. Put one in `~/.local/share/tuxistant/skills/` and it loads, changes and goes while Tux runs, with no restart. [docs/SKILLS.md](docs/SKILLS.md#code-skills) shows how to write one. Tapo lights now run this way, and find their bulbs again with a broadcast the first time after updating.
- **Home Assistant.** "Turn off the kitchen light", "Is the front door locked?" and "Set the heating to 21". It controls and reads lights, plugs, fans, heating, blinds, locks, scenes and scripts by the names they have in Home Assistant. Unlocking, opening, activating a scene and running a script show a card first. Add the address and a long-lived access token in Settings. Until both are set the model isn't offered it, so nothing changes for anyone without one. The token is never handed back, and a new address clears it.
- **Timers and reminders.** "Set a pasta timer for 8 minutes", "Remind me at 5pm to call mum" and "How long is left on the pasta?". Tux says it out loud when it is due and leaves a notification, and they are kept through a restart. On a locked screen there is only the notification, and the words come if you unlock within an hour for a timer, or a day for a reminder. After that only the notification remains. "Chime for timers" in Settings, which starts off, adds a short sound. The daemon now starts at login on every install, with or without the Fn helper, as a reminder can't be said if nothing is running.
- **Media.** "Pause the music", "Skip this song", "What's playing?", "Turn it up" and "Volume 40". It reaches players through MPRIS, which Spotify, VLC and browsers speak, though so far it has only driven stand-in players. The volume never goes above 100 percent.
- **Mail search.** "When did I book the flights?" or "What did my landlord say about the rent?" is answered from the mail Thunderbird keeps on this computer, with who sent it and when. It is off until you turn on "Search my mail" in Settings. Mail goes nowhere but your own server. Junk and Trash are skipped, by Thunderbird's own mark or by their name. Sign-in codes and passwords are hidden by the words around them and their shape, and [docs/ROADMAP.md](docs/ROADMAP.md) lists what gets past both. An answer from a sender who is new to you comes with a warning.
- **Facts from what's written down.** A question of general knowledge asked on an Fn hold is answered from Wikipedia, then from web search, with no click. Tux says it doesn't know when neither clearly answers, instead of guessing. It repeats what a page says, even when the page is wrong, and in the last facts evaluation one of 54 hard questions got a wrong answer. Sums are worked out on this computer. "Look up facts on Wikipedia" in Settings, which starts on, turns the lookup off.
- **Web search and research.** "Search the web for flights to Lisbon" reads out the top results, and "Research electric bikes" reads a few pages and tells you what they say, naming its sources. Both need a SearXNG server of your own with JSON output turned on, which Settings can point at.

### Fixed

Most of these fix code that is new in 0.3.0. They were found in tests and review before this release, not in use. Only the entries on lights, the calendar messages in Settings, and the key that the evaluations, the voice check and the demo video send change what 0.2.0 did.

- A timer or reminder whose words couldn't be spoken, for example with the speech server down, is no longer marked missed at once. It stays pending and is tried again after one, two and four minutes, and only then counts as missed. The error shows once for each try. Once some of the words were heard it counts as said, so it is never said twice.
- The timer chime plays to its end however the words fail, such as a refused connection or a timeout, not only on a server error. A key press still cuts it.
- A timer or reminder whose words were cut by the screen locking before any was heard is no longer recorded as said. It is said after the unlock, late if it has to be.
- One timer waiting to be tried again after its words failed no longer holds back the ones due after it. They are said on time.
- If the timer scheduler ever fails, it is logged and starts again, so timers keep working for the rest of the session.
- In the daemon as built, the timer chime would never have played on a locked screen, and a timer that came due while the screen was unlocked would have been marked missed instead of said. A setting was read by a name that was lost when the daemon's start-up code was split in two. A test caught it before any timer ran in use.
- If the screen-lock check fails while it is being set up, speech stays locked, so a timer shows a notification and is not said aloud on what may be a locked screen.
- A damaged `timers.json` holding an absurd id or time loses only that row. A due time in the last days of the year 9999 no longer stops every timer listing.
- A saved id counter at its very top no longer hands out ids that the next start drops.
- Lights are offered to the model only once a TP-Link email and password are in Settings.
- A Home Assistant switch or helper whose name or id says alarm, siren, lock, security, arm, disarm or deadbolt, or a garage, door or gate that isn't also a light, lamp, bulb, heater or fan, now waits for a click whichever way it is switched. The name and the id are judged apart, and joined spellings such as Doorlock, Frontdoor and Garageopener count. A long name is judged past the 40 characters the card shows, and a card is shown if either the shown or the longer name says so.
- Questions about the music playing, timers and the home that use words such as my, our, this song, timer, time left, locked, front door or the heating, like "How much time is left?", "Is the kettle on?" or the temperature in the kitchen, now go the normal way without being sorted, so the sorter doesn't send them to Wikipedia, though in this release the voice model can still pick its lookup for one, which sends it there. The release after this one fixes that. One worded without any of these, such as "Who's at the door?", still depends on the sorter: in the last measured run it called "What colour is the lamp?" a fact once, which sends the question to Wikipedia.
- Without Evolution Data Server's Python bindings, Settings now names the packages to install, evolution-data-server and gir1.2-ecal-2.0, in the same words the voice uses, instead of suggesting an Online Account.
- Settings no longer tells you to add an Online Account when it can't list your calendars, for example after a timeout or with a daemon that hasn't restarted since an update. It says the listing failed instead.
- The evaluations and the live protocol test send the key only to a server named for the run, or to one saved in Settings that is neither the default nor on this computer. They share one set of stand-ins, which now also blocks timer notifications and never starts a code skill's process.
- The voice check and the demo video now check the server before they send the key, like the evaluations. A demo video with no server the key may go to comes out silent. A server on this computer is refused unless it is named for the run: a loopback address in any spelling, such as 127.1, 2130706433 or ::ffff:127.0.0.1, a localhost name such as ip6-localhost, the machine's own name, and any address its network interfaces hold. Nothing is looked up, so another name that `/etc/hosts` gives this computer is not caught. The end-to-end harness now needs `TUXISTANT_SERVER` as well.
- Text from a code skill can no longer carry chat-template delimiters to the model. An answer or an error loses its `<` and `>`, and a skill whose name, description, version, action description or params text holds them is refused with a reason.
- `./uninstall.sh --purge` now also removes `~/.local/state/tuxistant`, where code skills keep what they save in `/state`, such as the addresses of the bulbs the lights found. Without `--purge` it is kept, and the uninstaller says so.
- A new test, `tests/test_names.py`, fails the suite on any name a function reads as a global that nothing defines. It reads every file under `tuxistant/` and `tools/` without running them, so it would have caught the timer chime bug above.

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
