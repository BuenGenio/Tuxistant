# Testing

## The layers

| Layer | Command | Needs |
|---|---|---|
| Everything local | `python3 -m unittest discover -s tests -t .` | Takes a few minutes, so run it in the background |
| One module | `python3 -m unittest tests.test_files` | |
| Real GNOME Shell, isolated | `tests/shell_harness/shot.sh <theme> <out.png>`, `tests/shell_harness/card.sh`, and `TUX_SCENARIO=dictation` or `dictation_real` for real typing into real windows | `gnome-shell`. Safe next to the desktop |
| A shell you can use | `tools/try-nested.sh` | `mutter-dev-bin`. Uses your real config, mic and speakers |
| End to end | `TUXISTANT_LIVE=1 TUXISTANT_SERVER=host:8080 TUXISTANT_API_KEY=... python3 tests/shell_harness/test_e2e_live.py` | LocalAI |
| Live protocol | `TUXISTANT_LIVE=1 TUXISTANT_SERVER=host:8080 TUXISTANT_API_KEY=... python3 -m unittest tests.test_realtime_live` | LocalAI |
| Which tool the model picks | `python3 tools/eval_routing.py` | LocalAI |
| Whole requests | `python3 tools/eval_flows.py "a request"` | LocalAI |
| Whether facts are right, or made up | `python3 tools/eval_facts.py` | LocalAI, the internet and SearXNG. Takes about 15 minutes, so run it in the background |
| A real Home Assistant | `TUX_HA_REAL=1 python3 -m unittest tests.test_homeassistant_real` | Docker and the `ghcr.io/home-assistant/home-assistant:stable` image, about 3.5 GB on disk. Starts its own container on 127.0.0.1, removes it afterwards |
| Do the voices say the words | `python3 tools/check_voices.py` | LocalAI |
| The pictures in the docs | `python3 tools/docs_images.py` | `gnome-shell`, `ffmpeg`, Pillow. Safe next to the desktop |

## The calendar

`tests/test_calendars.py` uses a stand-in, and fails if anything reaches for the real calendars. `tests/test_calendars_eds.py` starts a real Evolution Data Server under `dbus-run-session` with a temporary home, so it has one empty local calendar and no accounts, and drives the skill against it in two time zones. Its driver refuses to run if it finds any other calendar or any event. `tools/eval_flows.py` swaps in a made-up calendar, so an evaluation never reads the owner's week or adds to it.

## The mail

`tests/test_mail.py` builds a made-up copy of Thunderbird's index with `tests/mail_index.py`, and fails if anything opens an index outside its temporary folder. `tools/eval_flows.py` points mail search at a made-up mailbox from the same file, with a landlord, a dentist, a sign-in code and a scam in it, so an evaluation never reads real mail or sends any of it to a model. Keep it that way: every mail in there is invented, and none may be copied from a real inbox. Its senders use `mailbox.example` in place of a real mailbox provider, so that no address could be a real person's.

## Media

The opt-in live protocol test permits only `time.now`; every unexpected tool is blocked before its handler runs.

`tests/test_media.py` runs with the session bus address leading nowhere, the connection class swapped for one that fails the test, and a `PATH` that holds only a stand-in `wpctl`, so it can reach neither your players nor your volume. Its `PrivateBus` class starts stand-in players (`tests/media_fake_player.py`) on a bus from `dbus-run-session` and checks what each one was really sent. `tools/eval_flows.py` swaps in a made-up player and a made-up volume, and a test fails if it stops doing so. Nothing here has driven a real player or a real `wpctl`.

## Timers and reminders

`tuxistant/timers.py` has no default file: its three actions answer "Timers aren't available right now." and write nothing until a store is attached, and only the daemon attaches the real one. `tests/test_timers.py` uses stores in temporary folders and a clock that only a test moves, and sends its one notification to a stand-in on a bus from `dbus-run-session`. `tests/test_daemon_controller.py` runs the scheduler on the real controller with fake audio, and its notifications go to an object that only records them. `tests/test_daemon_dbus.py` runs the real `service.run` on a private bus with a stand-in screen shield and a stand-in notification service, and refuses to start unless the config and the timers file are in the temporary folders. It removes both afterwards, because the daemons of the `Process` tests share those folders and theirs is the real speaker: never leave a timer due there. `tools/eval_flows.py` attaches a store in a temporary folder and runs no scheduler, so nothing set in an evaluation is ever said, and a test fails if that changes. No real notification has been shown and the chime has never been heard.

## Home Assistant

`tests/test_homeassistant.py` uses a fake server on localhost and patches `socket` so that any test reaching another address or looking up a name fails. `tools/eval_flows.py` swaps in a made-up home, blocks the one function that opens a connection, and hands the skill a made-up address and token whatever Settings holds, so an evaluation can never switch a real light or say whether a real door is locked. A test fails if any of those three lines goes, or if the module gains a second way to connect. `tests/test_homeassistant_real.py` only runs when asked, against a container it starts and removes itself. It was changed when activating a scene got a card and has not been run since.

## After changing a tool

Unit tests can't tell you what a 4B model will do, so run `tools/eval_routing.py` after adding a tool or changing a description. It runs each request three times and executes nothing. If the saved config names no model server, set `TUXISTANT_SERVER=host:port` or pass `--server host:port` for the run; the evaluation refuses an implicit localhost default before sending a key.

It builds the tool list from defaults plus made-up accounts, in memory only, so the setup is chosen for the run and Settings are never read: `TUX_EVAL_HOME=1` adds Home Assistant, and `TUX_EVAL_MAIL=0` or `TUX_EVAL_TAPO=0` take away mail search or the Tapo lights, which are on by default. A case whose tools are all off in that setup is left out, and with Home Assistant and no Tapo account the light cases expect Home Assistant's tools. Run every setup you change something for: a word that helps one can cost another.

The run of 2026-11-13, with three repeats, before and after the description changes that came with it (26 requests and one fact were added first, so earlier numbers are not comparable; "Before" with both light providers counts the two new named lights as right with either):

| Setup | Tools | Before | After | Facts, three repeats | Facts, ten repeats |
|---|---|---|---|---|---|
| Tapo, as installed | 29 | 408/432 | 411/432 | 37/45 to 37/45 | 120/150 to 123/150 |
| Tapo and mail search | 30 | 428/456 | 436/456 | 32/45 to 34/45 | 110/150 to 108/150 |
| Tapo and Home Assistant | 31 | 476/519 | 484/519 | 40/51 to 37/51 | 131/170 to 141/170 |
| Home Assistant only | 29 | 465/519 | 477/519 | 43/51 to 41/51 | 127/170 to 124/170 |

Facts are asked on a turn from the shortcut, as a hold turn answers them before this model hears them. They move by several from run to run, so the ten-repeat column, 488 to 496 of 640, is the one to judge a change by. Three repeats of the routing cases move by a few, so don't read much into 2 or 3.

What changed and why: shell now says "install or update", so "Update my system" stops reporting uptime. Media control lists "next" and says a single word counts, so "Skip" works. "What's playing" no longer offers "who sings this", which sent "Who sang Hey Jude?" to the player. Home Assistant names the TV, which went to pause the music. File open says it is not for apps. The Tapo lights keep "the lamp" when no room is named, which Home Assistant had started taking. The calendar's "next" became "next event". A wording that listed "making a folder" and "shutting down" for shell fixed both, but cost about a tenth of the facts in the Home Assistant setup, so it was dropped; wording for the alarm, for locking, and "it can't open apps" for media helped nothing and was dropped too.

Still wrong in every setup: "Open Spotify" and "Play some jazz" press play, "Cancel my 3pm meeting" reads the calendar, "Turn off the lights in ten minutes" sets a spoken reminder, and "Next" and "Resume" on their own get no tool or a timer. With Home Assistant: "What's the temperature?" asks the home, not the weather, "Disarm the alarm" checks the locks, and "Play some jazz in the kitchen" presses play. With Home Assistant only: "Lock the front door" and "Unlock the front door" only check it, "Make a folder" measures the folder, and "Who sings this?" mostly gets no tool. Plain facts the model gets wrong or refuses are mostly the same few: "What's 15 percent of 80?", "How long does it take to boil an egg?", the author of Pride and Prejudice and the year the Titanic sank. The model often says "Use the knowledge_answer tool" instead of calling it.

From the earlier round, on 2026-11-11: a separate sorter check classified all 73 tested media, timer and Home Assistant requests as OTHER, with no false facts. Nine whole-request checks used a made-up player, volume, timers file and home; every other tool was blocked. They covered playback, setting/listing/cancelling a timer, home state and declined unlock/scene cards. They did not speak through the real audio output or reach real devices.

The final isolated suite reported 2,232 tests, passing with 164 skips. All changed code and test files matched that checkout. Seventeen deliberate regressions were caught in throwaway copies, after their tests first passed unbroken. The test fixes included preserving user-installed Python dependencies in the installer subprocess and pinning timer fixtures to UTC. No real desktop, device, audio, notification or user calendar was exercised. The Home Assistant Docker test and real-desktop checks remain unrun.

Run it after changing the instructions too. It also asks plain facts and checks the reply, because "no tool" alone can't tell an answer from "I can't do that". That gap once hid Tux refusing 44 of 70 simple questions behind a good routing score.

The model is consistent: a request that goes wrong usually goes wrong all three times. One tool's wording can also move requests that seem unrelated, so always run all of it. When a real request goes wrong, add it as a case.

Then run `tools/eval_flows.py`, which follows whole requests through the real tools. It finds what routing can't, such as a reply Tux shouldn't say aloud, or a result that loses its caveat on the way to the model.

Run `tools/eval_facts.py` after changing `tuxistant/knowledge.py` or anything the sorter sees. Two numbers matter: wrong answers, and other requests called a fact, as those go to Wikipedia with no click. Both must be 0. Its lists exist for reasons that each cost a wrong conclusion:

- **Hard questions and rewordings.** A check that scored 43 of 46 on easy facts missed 32 of 116 once they were reworded.
- **A list that was never tuned on.** The sorter's examples came from looking at its misses, so its score on those lists flatters it. Read the "never seen" line, and keep it unseen: if one of those goes wrong, move it up and write a new one.
- **Failures counted apart from not knowing.** An early run scored search engine errors as "I don't know" and proved nothing.

Search engines block a server that asks too fast, for hours. That is why it pauses between questions, and a search that fails here says nothing about the code. Check the engines one at a time before believing a bad score.

## Staying off the real desktop

All of these really happened:

- **A stub stopped covering the code.** Tests stubbed `files._run` to keep `gio open` away from the desktop. A new `_launch` used `subprocess.run` directly, and the tests opened 61 real processes, including LibreOffice and Papers. **Before replacing anything a test stubs, check the stub still covers it.**
- **"Cards are declined" isn't "nothing happens".** Actions with no card really run. In a live run the model chose to open the owner's CV on screen. `tools/eval_flows.py` now cuts off opening files and phone actions. **Any new action with no card that touches the desktop or a device must be cut off there too.**
- **A borrowed script started with `cd ~`.** A helper reused from a review wrote fake secrets into the real home folder before anyone read its first line. They were found, checked byte for byte and removed. **Read any script before running it, even one from this project, and run it with `HOME` pointing at a temporary folder.**

Also:

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
