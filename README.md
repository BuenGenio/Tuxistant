# Tuxistant

A voice assistant for the Linux desktop. Press a shortcut or hold Fn, talk, and Tux answers out loud while an animated blob reacts on screen and captions show what was heard and said.

Speech recognition, the language model and speech synthesis run on a [LocalAI](https://localai.io) server of your own. Tuxistant is the desktop side: a GNOME Shell extension, a small daemon and a Settings app.

## Requirements

- GNOME Shell 50 on Wayland
- Python 3.14 with `dbus_fast`, `websockets`, `PyYAML` and PyGObject (GTK 4, libadwaita 1.9 or newer)
- PipeWire with `pw-record` and `pw-cat`
- `glib-compile-schemas`
- A LocalAI server with a realtime pipeline (`tux-realtime` by default) and a voice model (`tux-voice`)
- For the skills, all optional: `bwrap` for shell commands that need no click, `localsearch` or `plocate` for finding files, GSConnect for phones, a LibreTranslate server for translation, and Piper voices on LocalAI to hear translations spoken

## Install

```sh
./install.sh                  # everything, including hold-Fn-to-talk; the Fn helper uses sudo
./install.sh --no-fn-helper   # the same without hold-Fn-to-talk, so no sudo
```

Then finish by hand, because a running Wayland session cannot load a new extension:

1. Log out and back in once.
2. Run `gnome-extensions enable tuxistant@buengenio`.
3. Run `tuxistant doctor` to check that everything is in place, including that the extension is enabled.

The installer puts a `tuxistant` command in `~/.local/bin`, the extension in `~/.local/share/gnome-shell/extensions`, a systemd user unit and a D-Bus activation file in `~/.local/share`, and creates `~/.config/tuxistant/config.json`. Set the server and API key in Settings under General. To have the installer fetch the key, set `TUXISTANT_KEY_COMMAND` to a command that prints it.

The `tuxistant` command runs from this checkout, so keep the folder where it is. Run `./install.sh` again after updating it. Running it twice is harmless and keeps your settings.

To remove everything, including the Fn helper, run `./uninstall.sh`. Your settings, API key, history and themes stay unless you add `--purge`.

## Use

| To talk | Do this |
|---|---|
| Shortcut | `Ctrl+Super+Space` starts listening, and Tux works out when you have finished. Press it again to cancel |
| Hold Fn | Hold Fn for 1.5 seconds, talk, then let go. Pressing another key during those 1.5 seconds cancels, so Fn shortcuts keep working. Needs the Fn helper |
| Panel icon | The microphone icon has Talk or Stop, Theme, your recent requests (click one to copy its reply) and Settings |

During a turn you can also right-click the blob to switch theme, open Settings or stop. A band theme such as Waves lets clicks through to the windows below it, so use the panel icon there.

Nothing listens or speaks while the screen is locked: a turn in progress stops, and the shortcut, Fn and `tuxistant say` do nothing until you unlock.

## What you can ask

| Ask | What happens |
|---|---|
| "What's the weather?" or "What's the news?" | Reads it live. Set your home town in Settings for weather |
| "Find my 2024 tax return" | Searches file names and contents, and reads out the best few. "Open the second one" works for a few minutes afterwards |
| "Send it to my phone", "Where's my phone?" | Sends a file through GSConnect, rings the phone, or reads its battery. Sending asks first |
| "Move everything in Downloads older than a year to the Trash" | Shows how many files and how much space, and waits for your click. Nothing is deleted outright |
| "How do you say thank you in Ukrainian?" | Translates on your own server and says it in a native voice. "Translate what I've selected into French" works too |
| "What's my IP address?", "Which folder is the biggest?", "Is docker running?" | Reads it from the system |
| "What does my todo file say?", "Install VLC" | Runs a command. Read-only commands run in a sandbox with no click, and anything else shows the exact command first |
| "Find my CV and send it to my phone" | Several steps in a row, with the same approval cards |

**Approval cards.** Anything that changes something, or sends a file off this computer, puts a card on screen saying exactly what will happen, and only a click on Allow goes ahead. Saying yes doesn't count, because a voice nearby could say it. No answer in 45 seconds counts as no. Every action and answer is listed in History. [docs/SKILLS.md](docs/SKILLS.md) has the rules.

**Cloud.** Complex requests stay on your own server unless you turn on Cloud in Settings, which is off by default and needs your own Anthropic API key. With it on, Claude sees your request plus file names and command output from that task. File contents go only if you click to allow each one. It needs the Anthropic SDK: run `./install.sh --cloud-sdk`.

**Settings** has pages for General (server, API key, listening, shortcut, phone, translation, cloud, instructions), Voice, Appearance, Skills and History.

The same things work from a terminal:

| Command | What it does |
|---|---|
| `tuxistant toggle` | Start listening, or cancel what is going on |
| `tuxistant cancel` | Stop everything |
| `tuxistant say <text>` | Speak some text |
| `tuxistant history` | Show recent requests |
| `tuxistant doctor` | Check the server, the API key, PipeWire, the Fn helper, that the extension is enabled, that the daemon can start, and that approval cards can show |
| `tuxistant settings` | Open Settings |
| `tuxistant daemon` | Run the daemon in the foreground |

## How it fits together

```
 GNOME Shell extension            tuxistant daemon                  LocalAI
 blob, captions, shortcut  <--->  turns, audio, skills,   <------>  speech, model,
 panel icon, menus        D-Bus   history, config        WebSocket  voice
                                    ^           ^
 Settings app  <--------------------+           |
                     D-Bus                      | Unix socket: down, up, other
                                         tuxistant-fn (system service, sandboxed)
```

- **Daemon** (`tuxistant/`): runs as a systemd user service and starts on demand through D-Bus. It owns the conversation, the microphone and speaker through PipeWire, skills, history and config. With the Fn helper installed it also starts at login, so that holding Fn works straight away.
- **Extension** (`extension/`): everything on screen. It talks to the daemon over the session bus.
- **Settings** (`tuxistant/settings_app.py`): a GTK 4 and libadwaita app that talks to the same daemon.
- **Themes** (`themes/`): a JSON file and a GLSL shader each. See [docs/THEMES.md](docs/THEMES.md).
- **Skills** (`tuxistant/skills_builtin/`): the actions Tux can take. See [docs/SKILLS.md](docs/SKILLS.md).
- **Fn helper** (`helper/`): a GNOME shortcut cannot be set to Fn alone, so a small separate service reads the key from the kernel.

[docs/CONTRACT.md](docs/CONTRACT.md) is the exact specification between these parts. [docs/ROADMAP.md](docs/ROADMAP.md) has where the project stands and what comes next.

## Privacy

- **The Fn helper is the only part that can see your typing, and it is built to forget it.** To notice Fn it has to read the keyboard. It says only `down`, `up` and `other`: `other` means some other key was pressed while Fn was held, at most once per hold, and it never says which. With Fn up it says nothing at all. It runs as an unprivileged throwaway user with no network, and `tests/test_fn_helper.py` checks these promises.
- The microphone is on only during a turn, plus a short while after each reply if conversation mode is on. Audio goes to the server you configured and nowhere else.
- Weather and news are the only skills that reach the internet, and only when you ask. A weather question sends the town name to Open-Meteo; a news question fetches a BBC News feed. Both services see your IP address, and neither receives your voice or the conversation. Cloud is the one exception, and it is off unless you turn it on. Translation stays on your own server.
- Once a conversation has read your files, command output or selected text, anything reaching past your own server needs a click.
- History stays on this machine in `~/.local/share/tuxistant/history.db`, readable only by you. You can clear it in Settings.
- The API key lives in `~/.config/tuxistant/config.json`, readable only by you. It is not sent over D-Bus to the extension or to Settings, and changing the server clears it, so that it only ever goes to the server it was given for. For TLS, enter the server as `https://host:port`.

## Development

```sh
python3 -m unittest discover -s tests -t .
```

[docs/TESTING.md](docs/TESTING.md) covers the test layers and the live evaluations. No test needs a microphone, a speaker or your GNOME session. The installer tests run in a temporary prefix with `ssh`, `sudo` and `systemctl` replaced by stand-ins. The audio tests run through a private PipeWire graph that has only a null sink.

With a LocalAI server to talk to, this also runs everything end to end in an isolated headless GNOME Shell: the real daemon, extension, Settings app and Fn helper, with scripted audio. Set `TUX_E2E_OUT` to a folder to keep its screenshots.

```sh
TUXISTANT_LIVE=1 TUXISTANT_API_KEY=... python3 tests/shell_harness/test_e2e_live.py
```

## Licence

Copyright 2026 Eugene Trotsan. Tuxistant is free software under the GNU General Public License, version 3. See [LICENSE](LICENSE).

## Credits

The bundled themes are original work, except that Listening Tux redraws, as a from-scratch shader, the Tux that Larry Ewing (lewing@isc.tamu.edu) made with The GIMP in 1996. That design may be used and modified provided Larry Ewing and The GIMP are acknowledged.
