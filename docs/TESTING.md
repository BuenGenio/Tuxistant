# Testing

## The layers

| Layer | Command | Needs |
|---|---|---|
| Everything local | `python3 -m unittest discover -s tests -t .` | Takes a few minutes, so run it in the background |
| One module | `python3 -m unittest tests.test_files` | |
| Real GNOME Shell, isolated | `tests/shell_harness/shot.sh <theme> <out.png>`, `tests/shell_harness/card.sh`, and `TUX_SCENARIO=dictation` or `dictation_real` for real typing into real windows | `gnome-shell`. Safe next to the desktop |
| A shell you can use | `tools/try-nested.sh` | `mutter-dev-bin`. Uses your real config, mic and speakers |
| End to end | `TUXISTANT_LIVE=1 TUXISTANT_SERVER=host:8080 TUXISTANT_API_KEY=... python3 tests/shell_harness/test_e2e_live.py` | LocalAI |
| Live protocol | `TUXISTANT_LIVE=1 TUXISTANT_SERVER=host:8080 TUXISTANT_API_KEY=... python3 -m unittest tests.test_realtime_live` | LocalAI. `TUXISTANT_SERVER` is required: without it the test fails rather than falling back to localhost |
| Which tool the model picks | `python3 tools/eval_routing.py` | LocalAI |
| Whole requests | `python3 tools/eval_flows.py "a request"` | LocalAI |
| Whether facts are right, or made up | `python3 tools/eval_facts.py` | LocalAI, the internet and SearXNG. Takes about 15 minutes, so run it in the background |
| A real Home Assistant | `TUX_HA_REAL=1 python3 -m unittest tests.test_homeassistant_real` | Docker and the `ghcr.io/home-assistant/home-assistant:stable` image, about 3.5 GB on disk. Starts its own container on 127.0.0.1, removes it afterwards |
| Do the voices say the words | `TUXISTANT_SERVER=host:port python3 tools/check_voices.py`, or `--server host:port` | LocalAI. Like the evaluations, it won't send the key to the default localhost:8080 or to any address on this computer unless that server is named for the run |
| Names nothing defines | `python3 -m unittest tests.test_names` | Nothing. It reads the symbol table of every file under `tuxistant/` and `tools/` and fails on a name read as a global that nothing defines, such as a lambda reading another function's local. It catches the timer chime bug without running the code. A star import fails it too |
| The pictures in the docs | `python3 tools/docs_images.py` | `gnome-shell`, `ffmpeg`, Pillow. Safe next to the desktop |

## The calendar

`tests/test_calendars.py` uses a stand-in, and fails if anything reaches for the real calendars. `tests/test_calendars_eds.py` starts a real Evolution Data Server under `dbus-run-session` with a temporary home, so it has one empty local calendar and no accounts, and drives the skill against it in two time zones. Its driver refuses to run if it finds any other calendar or any event. `tools/eval_cutoffs.py`, which both `tools/eval_flows.py` and `tests/test_realtime_live.py` install, swaps in a made-up calendar, so an evaluation never reads the owner's week or adds to it.

## The mail

`tests/test_mail.py` builds a made-up copy of Thunderbird's index with `tests/mail_index.py`, and fails if anything opens an index outside its temporary folder. `tools/eval_cutoffs.py`, which both `tools/eval_flows.py` and `tests/test_realtime_live.py` install, points mail search at a made-up mailbox from the same file, with a landlord, a dentist, a sign-in code and a scam in it, so an evaluation never reads real mail or sends any of it to a model. Keep it that way: every mail in there is invented, and none may be copied from a real inbox. Its senders use `mailbox.example` in place of a real mailbox provider, so that no address could be a real person's.

## Media

The opt-in live protocol test permits only `time.now`; every unexpected tool is blocked before its handler runs.

`tests/test_media.py` runs with the session bus address leading nowhere, the connection class swapped for one that fails the test, and a `PATH` that holds only a stand-in `wpctl`, so it can reach neither your players nor your volume. Its `PrivateBus` class starts stand-in players (`tests/media_fake_player.py`) on a bus from `dbus-run-session` and checks what each one was really sent. `tools/eval_cutoffs.py`, which both `tools/eval_flows.py` and `tests/test_realtime_live.py` install, swaps in a made-up player and a made-up volume, and a test fails if it stops doing so. Nothing here has driven a real player or a real `wpctl`.

## Timers and reminders

`tuxistant/timers.py` has no default file: its three actions answer "Timers aren't available right now." and write nothing until a store is attached, and only the daemon attaches the real one. `tests/test_timers.py` uses stores in temporary folders and a clock that only a test moves, and sends its one notification to a stand-in on a bus from `dbus-run-session`. `tests/test_daemon_controller.py` runs the scheduler on the real controller with fake audio, and its notifications go to an object that only records them. `tests/test_daemon_dbus.py` runs the real `service.run` on a private bus with a stand-in screen shield and a stand-in notification service, and refuses to start unless the config and the timers file are in the temporary folders. It removes both afterwards, because the daemons of the `Process` tests share those folders and theirs is the real speaker: never leave a timer due there. `tests/test_daemon_controller.py` also drives a speech server that is down, then comes back, through the real controller, and restarts the scheduler after a failure. `tools/eval_cutoffs.py`, which both `tools/eval_flows.py` and `tests/test_realtime_live.py` install, attaches a store in a temporary folder and blocks `timers.notify` there too. Nothing there runs the scheduler, so nothing set in an evaluation is ever said or shown, and a test fails if that changes. No real notification has been shown and the chime has never been heard.

## Home Assistant

`tests/test_homeassistant.py` uses a fake server on localhost and patches `socket` so that any test reaching another address or looking up a name fails. `tools/eval_cutoffs.py`, which both `tools/eval_flows.py` and `tests/test_realtime_live.py` install, swaps in a made-up home, blocks the one function that opens a connection, and hands the skill a made-up address and token whatever Settings holds, so an evaluation can never switch a real light or say whether a real door is locked. A test fails if any of those three stand-ins is not in place once installed, or if the module gains a second way to connect. `tests/test_homeassistant_real.py` only runs when asked, against a container it starts and removes itself. It was changed when activating a scene got a card and has not been run since.

## After changing a tool

Unit tests can't tell you what a 4B model will do, so run `tools/eval_routing.py` after adding a tool or changing a description. It runs each request three times and executes nothing. The evaluations send the stored key only to a server named for the run, with `TUXISTANT_SERVER=host:port` or `--server host:port`, or to the one saved in Settings when that is neither the default nor on this computer. "On this computer" means a loopback or unspecified address in any spelling, a localhost name, this computer's own name or `<name>.local`, or an address one of its network interfaces holds. Other names that `/etc/hosts` maps to this computer are not caught, as nothing is looked up. A server on this computer must be named for the run every time, because something else may be listening on localhost:8080.

It builds the tool list from defaults plus made-up accounts, in memory only, so the setup is chosen for the run and Settings are never read: `TUX_EVAL_HOME=1` adds Home Assistant, and `TUX_EVAL_MAIL=0` or `TUX_EVAL_TAPO=0` take away mail search or the Tapo lights, which are on by default. A case whose tools are all off in that setup is left out, and with Home Assistant and no Tapo account the light cases expect Home Assistant's tools. Run every setup you change something for: a word that helps one can cost another. The daemon itself offers the Tapo tools only with a TP-Link email and password, and Home Assistant's only with an address and token; `tests.test_skills.Offered` checks which are offered with neither, either or both accounts.

The run of 2026-11-13, at commit b2e9493, three repeats, after the description changes listed below. Every number is what the command printed, with no rescoring. Each row names the server for the run, here `--server host:port`:

| Setup | Command | Tools | Routing | Facts |
|---|---|---|---|---|
| Tapo, as installed | `TUX_EVAL_MAIL=0 TUX_EVAL_TAPO=1 TUX_EVAL_HOME=0 python3 -u tools/eval_routing.py --server host:port 3` | 29 | 411/432 | 36/45 |
| Tapo and mail search | `TUX_EVAL_MAIL=1 TUX_EVAL_TAPO=1 TUX_EVAL_HOME=0 python3 -u tools/eval_routing.py --server host:port 3` | 30 | 435/456 | 35/45 |
| Tapo and Home Assistant | `TUX_EVAL_MAIL=0 TUX_EVAL_TAPO=1 TUX_EVAL_HOME=1 python3 -u tools/eval_routing.py --server host:port 3` | 31 | 484/519 | 40/51 |
| Home Assistant only | `TUX_EVAL_MAIL=0 TUX_EVAL_TAPO=0 TUX_EVAL_HOME=1 python3 -u tools/eval_routing.py --server host:port 3` | 29 | 477/519 | 32/51 |

Mail search is on unless set to 0, so `TUX_EVAL_HOME=1` alone gives 32 tools and 543 tries, not the third row. With both light providers set up, every named light accepts either provider's tool, so these totals can't be compared with runs scored before that rule. Facts are asked on a turn from the shortcut, as a hold turn answers them before this model hears them. They move by several from run to run, so judge a change by more repeats: the same command with 10 in place of 3 asks each fact ten times, and runs the routing cases ten times too. Three repeats of the routing cases move by a few, so don't read much into 2 or 3.

`python3 tools/eval_facts.py --server host:port`, run straight after on the same server, gave no wrong answers on easy facts (46 of 48 right, 2 not known), one wrong on hard ones ("What is the smallest country in Africa?" got The Gambia; 51 of 54 right, 2 not known), all 12 made-up questions answered as not known, and 6 of 6 sums. Other requests were called a fact 7 times. Wrong answers and requests called a fact must both be 0, so this run failed (it exits 1). The 7 were: in the tuned list "What's the date today?" and "What can you do?" twice each, in the never-seen list "What's the latest iPhone?" twice (2 of 84 tries, with no facts missed), and among the routing requests "What colour is the lamp?" once.

The word filter then took words that only hold now (today, latest, currently and the like), questions about Tux itself, and the colour or brightness of a lamp, light or bulb, written as shapes rather than from the leaked cases; the never-seen one was left to those general shapes. None of the 81 facts in `eval_facts.py` or the 17 in `eval_routing.py` matches it. A fresh run of `python3 tools/eval_facts.py --server host:port` on 2026-11-13 called nothing else a fact: 0 of 86 tries in the tuned list, 0 of 84 in the never-seen list and 0 of 360 routing requests, with no facts missed (114 and 48 tries). Easy facts: 44 of 48 right, 4 not known, none wrong. Hard: 52 of 54 right, 1 not known, and the same one wrong ("What is the smallest country in Africa?" got The Gambia, the smallest on the mainland). Made up: 12 of 12 not known. Sums: 6 of 6. That one wrong answer still makes it exit 1.

What changed and why: shell now says "install or update", so "Update my system" stops reporting uptime; it went to shell 3 of 3 in every setup. Media control lists "next" and says a single word counts, so "Skip" works. "What's playing" no longer offers "who sings this", which sent "Who sang Hey Jude?" to the player. Home Assistant names the TV, which went to pause the music. File open says it is not for apps, which did not fix "Open Firefox". The Tapo lights keep "the lamp" when no room is named, which Home Assistant had started taking. The calendar's "next" became "next event". A wording that listed "making a folder" and "shutting down" for shell fixed both, but cost about a tenth of the facts in the Home Assistant setup, so it was dropped; wording for the alarm, for locking, and "it can't open apps" for media helped nothing and was dropped too.

Still wrong in every setup: "Open Spotify" and "Play some jazz" press play, "Cancel my 3pm meeting" reads the calendar, "Turn off the lights in ten minutes" sets a spoken reminder, and "Next" and "Resume" on their own get no tool, a timer or the time. "Open Firefox" opens a file or runs a shell command in three setups, and got no tool 3 of 3 only with Tapo and Home Assistant. With Home Assistant, in both setups: "Make a folder called Invoices in my Documents" measures the folder, "What's the temperature?" asks the home, not the weather, "Disarm the alarm" checks the locks or the alarm, "Play some jazz in the kitchen" presses play, and "Turn off the computer" gets no tool 3 of 3 with Tapo and 2 of 3 without. With Tapo and Home Assistant, "Are any windows open?" went to the Tapo lights 2 of 3. With Home Assistant only: "Lock the front door" and "Unlock the front door" mostly only check it, "Who sings this?" got no tool 2 of 3, and "Open the living room curtains" checked them once in 3. Plain facts the model gets wrong or refuses are mostly the same few: "How long does it take to boil an egg?", "Who sang Hey Jude?" and the author of Pride and Prejudice (Charlotte Brontë) in every setup, the year the Titanic sank (2012) in most, "Who wrote Bohemian Rhapsody?" with mail search and with Tapo and Home Assistant, and "What's 15 percent of 80?" with Home Assistant. With Home Assistant only, the Mona Lisa, the minutes in a day and the light bulb were missed as well, which gives the 32 of 51 there; that is one run of three repeats, so it may be noise. The model often says "Use the knowledge_answer tool" instead of calling it.

From the earlier round, on 2026-11-11: a separate sorter check classified all 73 tested media, timer and Home Assistant requests as OTHER, with no false facts. Nine whole-request checks used a made-up player, volume, timers file and home; every other tool was blocked. They covered playback, setting/listing/cancelling a timer, home state and declined unlock/scene cards. They did not speak through the real audio output or reach real devices.

The final isolated suite reported 2,232 tests, passing with 164 skips. All changed code and test files matched that checkout. Seventeen deliberate regressions were caught in throwaway copies, after their tests first passed unbroken. The test fixes included preserving user-installed Python dependencies in the installer subprocess and pinning timer fixtures to UTC. No real desktop, device, audio, notification or user calendar was exercised. The Home Assistant Docker test and real-desktop checks remain unrun.

Run it after changing the instructions too. It also asks plain facts and checks the reply, because "no tool" alone can't tell an answer from "I can't do that". That gap once hid Tux refusing 44 of 70 simple questions behind a good routing score.

The model is consistent: a request that goes wrong usually goes wrong all three times. One tool's wording can also move requests that seem unrelated, so always run all of it. When a real request goes wrong, add it as a case.

Then run `tools/eval_flows.py`, which follows whole requests through the real tools. It finds what routing can't, such as a reply Tux shouldn't say aloud, or a result that loses its caveat on the way to the model.

Run `tools/eval_facts.py` after changing `tuxistant/knowledge.py` or anything the sorter sees. Two numbers matter: wrong answers, and other requests called a fact, as those go to Wikipedia with no click. Both must be 0. Its lists exist for reasons that each cost a wrong conclusion:

- **Hard questions and rewordings.** A check that scored 43 of 46 on easy facts missed 32 of 116 once they were reworded.
- **A list that was never tuned on.** The sorter's examples came from looking at its misses, so its score on those lists flatters it. Read the "never seen" line, and keep it unseen: if one of those goes wrong, move it up and write a new one. The never-seen set holds music, timer and home phrasings that neither the sorter's examples nor the word filter were written from; don't tune either on them.
- **Failures counted apart from not knowing.** An early run scored search engine errors as "I don't know" and proved nothing.

Search engines block a server that asks too fast, for hours. That is why it pauses between questions, and a search that fails here says nothing about the code. Check the engines one at a time before believing a bad score.

## Staying off the real desktop

All of these really happened:

- **A stub stopped covering the code.** Tests stubbed `files._run` to keep `gio open` away from the desktop. A new `_launch` used `subprocess.run` directly, and the tests opened 61 real processes, including LibreOffice and Papers. **Before replacing anything a test stubs, check the stub still covers it.**
- **"Cards are declined" isn't "nothing happens".** Actions with no card really run. In a live run the model chose to open the owner's CV on screen. `tools/eval_cutoffs.py` now cuts off opening files and phone actions. **Any new action with no card that touches the desktop or a device must be cut off there too.**
- **A borrowed script started with `cd ~`.** A helper reused from a review wrote fake secrets into the real home folder before anyone read its first line. They were found, checked byte for byte and removed. **Read any script before running it, even one from this project, and run it with `HOME` pointing at a temporary folder.**

Also:

- `tests/test_realtime_live.py` installs the same stand-ins as `tools/eval_flows.py` and gives every action but the clock an error. `tools/eval_cutoffs.py` names every action and what it meets in an evaluation, and `tests/test_eval_config.py` fails when a new action is not named there. Code skills get no process in either.
- Use a temporary `HOME` for anything that changes files. Fake `gio` with a script on `PATH` to test real launching.
- If a test leaves `Directory not empty` in a temp folder, a real app may have written there. Check for stray processes before carrying on.
- Never run `gsettings set` or `systemctl --user start` in a test, even in a sandbox you believe holds.
- Don't read `~/.local/share/Trash`, even from inside the sandbox.
- When checking that a secret is masked, assert it's absent or empty and print nothing from it.
- A throwaway `gnome-shell` needs its own `XDG_RUNTIME_DIR`, or its at-spi launcher deletes the real session's bus.

## Does the test really test it?

A passing test proves nothing until you've seen it fail. For anything security-related, break the code in a throwaway copy and confirm the test catches it.

Run the same tests in that copy with nothing broken first, and stop if they fail. One run reported 18 of 18 mutants caught when every test was dying at import, because a temporary `HOME` hid `websockets` in the user's site-packages. Set `PYTHONUSERBASE` to the real `~/.local` when changing `HOME`, and `XDG_CONFIG_HOME` to the real `~/.config` when an eval needs the real config. A fresh worktree also needs `glib-compile-schemas extension/tuxistant@buengenio/schemas`.

A test that something didn't happen has to wait until it could have happened. Dictation's delivery runs apart from the job, and tests that asserted "nothing was sent" straight after the job ended passed whatever the code did. Breaking the guards is what showed it.

This found a real gap: a new class in `tests/test_daemon_dbus.py` was skipped and everything looked green, because the private-bus runner only runs the classes named in `RunsOnPrivateBus`. **Add a new class there to that list.** The same check proved that a guard matching on the D-Bus interface can be bypassed by leaving the interface out, which is why `Daemon.guard` matches on the method name alone.

## The pictures in the docs

`tools/docs_images.py` rebuilds `docs/images` after a change to anything on screen. The extension, Settings and the card wording are real, in the same isolated shell as `card.sh`. The server, history and requests are made up in `tests/shell_harness/docs_shots.py`, so no picture shows a real name, file or address. Keep it that way, and look at every picture before it goes in: this repo is public.

## Adversarial review

The [README](../README.md#adversarial-review) has how a review runs, what it has caught and what it doesn't cover. Each big round ends with one. A finding only counts once it's reproduced, and it ends as a test in `tests/test_review_regressions.py` or `tests/test_shellgate.py`.

## Attacking the shell gate

`tests/test_shellgate.py` holds the attacks as tests, against real bubblewrap. When changing the parser, the allowlist or the sandbox, think like a hostile file name, document or repo that has already reached the model. The model can be made to emit any tool call, so the parser, the sandbox and the card have to hold on their own.
