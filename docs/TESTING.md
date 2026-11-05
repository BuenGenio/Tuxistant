# Testing

## The layers

| Layer | Command | Needs |
|---|---|---|
| Everything local | `python3 -m unittest discover -s tests -t .` | Takes a few minutes, so run it in the background |
| One module | `python3 -m unittest tests.test_files` | |
| Real GNOME Shell, isolated | `tests/shell_harness/shot.sh <theme> <out.png>`, `tests/shell_harness/card.sh`, and `TUX_SCENARIO=dictation` or `dictation_real` for real typing into real windows | `gnome-shell`. Safe next to the desktop |
| A shell you can use | `tools/try-nested.sh` | `mutter-dev-bin`. Uses your real config, mic and speakers |
| End to end | `TUXISTANT_LIVE=1 TUXISTANT_API_KEY=... python3 tests/shell_harness/test_e2e_live.py` | LocalAI |
| Live protocol | `TUXISTANT_LIVE=1 python3 -m unittest tests.test_realtime_live` | LocalAI |
| Which tool the model picks | `python3 tools/eval_routing.py` | LocalAI |
| Whole requests | `python3 tools/eval_flows.py "a request"` | LocalAI |
| Do the voices say the words | `python3 tools/check_voices.py` | LocalAI |
| The pictures in the docs | `python3 tools/docs_images.py` | `gnome-shell`, `ffmpeg`, Pillow. Safe next to the desktop |

## After changing a tool

Unit tests can't tell you what a 4B model will do, so run `tools/eval_routing.py` after adding a tool or changing a description. It runs each request three times and executes nothing. The last run scored 113 of 120 with 17 tools, at about 0.4 s per request, and answered 26 of 30 facts.

Run it after changing the instructions too. It also asks plain facts and checks the reply, because "no tool" alone can't tell an answer from "I can't do that". That gap once hid Tux refusing 44 of 70 simple questions behind a good routing score.

The model is consistent: a request that goes wrong usually goes wrong all three times. One tool's wording can also move requests that seem unrelated, so always run all of it. When a real request goes wrong, add it as a case.

Then run `tools/eval_flows.py`, which follows whole requests through the real tools. It finds what routing can't, such as a reply Tux shouldn't say aloud, or a result that loses its caveat on the way to the model.

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

Run the same tests in that copy with nothing broken first, and stop if they fail. One run reported 18 of 18 mutants caught when every test was dying at import, because a temporary `HOME` hid `websockets` in the user's site-packages. Set `PYTHONUSERBASE` to the real `~/.local` when changing `HOME`.

A test that something didn't happen has to wait until it could have happened. Dictation's delivery runs apart from the job, and tests that asserted "nothing was sent" straight after the job ended passed whatever the code did. Breaking the guards is what showed it.

This found a real gap: a new class in `tests/test_daemon_dbus.py` was skipped and everything looked green, because the private-bus runner only runs the classes named in `RunsOnPrivateBus`. **Add a new class there to that list.** The same check proved that a guard matching on the D-Bus interface can be bypassed by leaving the interface out, which is why `Daemon.guard` matches on the method name alone.

## The pictures in the docs

`tools/docs_images.py` rebuilds `docs/images` after a change to anything on screen. The extension, Settings and the card wording are real, in the same isolated shell as `card.sh`. The server, history and requests are made up in `tests/shell_harness/docs_shots.py`, so no picture shows a real name, file or address. Keep it that way, and look at every picture before it goes in: this repo is public.

## Adversarial review

The [README](../README.md#adversarial-review) has how a review runs, what it has caught and what it doesn't cover. Each big round ends with one. A finding only counts once it's reproduced, and it ends as a test in `tests/test_review_regressions.py` or `tests/test_shellgate.py`.

## Attacking the shell gate

`tests/test_shellgate.py` holds the attacks as tests, against real bubblewrap. When changing the parser, the allowlist or the sandbox, think like a hostile file name, document or repo that has already reached the model. The model can be made to emit any tool call, so the parser, the sandbox and the card have to hold on their own.
