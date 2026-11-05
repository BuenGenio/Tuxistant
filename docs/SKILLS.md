# Skills

A skill is something Tux can do for you, such as telling the time. It declares typed actions and the permissions it needs. The model sees the actions as tools and calls them during a conversation; the daemon runs them and hands the result back to be spoken.

## What exists today

Built-in skills, in `tuxistant/skills_builtin/`:

| Skill | Actions | Permissions | Risk |
|---|---|---|---|
| Time, Disk | `time.now`, `disk.usage` | none | low |
| This computer | `system.info`, `system.running` | none | low |
| Folder sizes | `disk.folders` | `files_read` | medium |
| Weather, News | `weather.forecast`, `news.headlines` | `network` | medium |
| Files | `file.find`, `file.open` | `files_read` | medium |
| Trash | `file.trash` | `files_write` | high |
| Phone | `phone.ring`, `phone.battery` | none | low |
| Phone send | `phone.send` | `files_read` | medium |
| Translate | `translate.text`, `translate.selection` | `network: [home]`, and `clipboard` for the selection | medium |
| Shell | `shell.run` | `shell` | high |
| Task | `task.run` | `shell` | high |

A skill with a `home` network permission only talks to the server this install already uses. Settings lists each skill with its permissions and risk:

<img src="images/settings-skills.png" width="640" alt="The Skills page in Settings: each skill with what it can do, what it is allowed to do and its risk.">

Every action needs a Python handler registered in `HANDLERS` in `tuxistant/skills.py`. A manifest alone is not enough: an action without a handler returns an error for the model to report. That means skills can currently only be added by changing Tuxistant itself. There is no folder for user skills yet.

## Manifest format

A skill is one YAML file. This is the shape of a skill that needs the network:

```yaml
id: org.example.tides
name: Tides
description: Tells you the tide times
version: 1.0.0
permissions:
  network: [api.example.com]
actions:
  tides.today:
    description: Get today's tide times for a place
    params:
      place: { type: string, description: "City name" }
    required: [place]
```

| Field | Meaning |
|---|---|
| `id` | Unique reverse-domain name |
| `name` | Shown in Settings |
| `description` | Shown in Settings |
| `version` | Version of the skill |
| `permissions` | What the skill may do, see below. Use `{}` for nothing |
| `actions` | The actions, by name |

Each action has:

| Field | Meaning |
|---|---|
| `description` | Tells the model when to use it, so write it for the model |
| `params` | JSON Schema properties for the arguments. Optional |
| `required` | Names of the arguments that must be present. Optional |

Action names look like `weather.today`. The model sees them with the dot replaced by an underscore, so two names must not differ only in that way.

A handler takes the arguments, the config and the session, and returns a dict that can be turned into JSON, or an `Ask` when it needs a click first. If it raises, the message goes to the model as `{"error": ...}`, so a failed action becomes something Tux can say rather than a crash. "Approvals" in [CONTRACT.md](CONTRACT.md) has the details.

## Permissions

| Permission | Grants | Risk |
|---|---|---|
| `shell` | Running commands | high |
| `files_write` | Changing files | high |
| `files_read` | Reading files | medium |
| `network` | Reaching the network | medium |
| `clipboard` | Using the clipboard | medium |

Set a permission to `true` to ask for it. Names not in this table have no effect on risk.

The permissions set the risk, show up in Settings and drive the rule below. They don't yet confine a handler, because the only handlers are the built-in ones. Confinement comes with the skill types below.

## What needs a click

`Skills.execute` is the only way an action runs, whether the voice model or the planner asked for it.

- **Anything risky.** A handler returns an `Ask` instead of acting. The card shows what will really happen, written by Tuxistant from the real action, and only a click on it goes ahead. A spoken yes doesn't count, because audio playing nearby could say it. No answer in 45 seconds, a cancel or a locked screen all mean no.
- **Private data leaving.** Once a conversation has read files, command output or the selection, any action that reaches past your own server needs a click, whichever skill asks. One skill can't read your files and another send them out.
- **What runs with no click.** Reading, such as `file.find`, and shell commands the parser can prove are read-only. Those run in a sandbox with a read-only filesystem, no network and your secrets hidden.

Every action, every card and the answer to it are in the request history, including what the planner did.

| A normal card | A card for something that runs as root |
|---|---|
| ![A card asking to move 3 files and 1 folder to the Trash, listing each one, with Deny and Allow buttons.](images/card-trash.png) | ![A red card asking to run apt-get install as administrator, with Deny and Allow as administrator buttons.](images/card-admin.png) |

## The shell skill

`shell.run` is local and built in, and community skills will never be able to request it.

1. A strict parser reads the command. Anything it doesn't fully understand is not treated as read-only.
2. A command made only of known read-only programs runs in bubblewrap with no click. The parser judges intent and the sandbox is what holds if it is wrong. The sandbox has a read-only filesystem, no network, a seccomp filter and a memory limit, and it hides every dot-entry in the home folder except `.gitconfig`, along with files named like secrets elsewhere. Git runs with a repo's hooks, filters and helpers switched off.
3. Anything else shows the exact command on a card, and approval covers that command once.
4. Deleting previews first and uses the Trash where it can. Anything it can't move to the Trash says it can't be undone.
5. `sudo` alone on a line gets its own warning and runs through `pkexec`. Inside a longer command it is refused.

## Multi-step requests

`task.run` handles requests that need several steps. It runs one step, reads the result and picks the next, with at most 8 steps, and every action goes through the same gates.

It uses the model on your own server unless you turn on Cloud in Settings, which is off by default and needs your Anthropic API key. With it on, Claude sees the request plus file names and command output from that task. File contents go only if you click to allow each one.

## Computed risk

`risk_of()` in `tuxistant/skills.py` works out the risk from the permissions:

- **high** if the skill asks for `shell` or `files_write`
- **medium** if it asks for `files_read`, `network` or `clipboard`
- **low** otherwise

The risk is never read from the manifest. A `risk:` line is overwritten when the skill loads, so an author cannot label a dangerous skill as safe.

## Planned

None of this exists yet.

- **Recipe skills.** Declarative steps with no code of their own. These are the ones meant to be shared, because what they do can be read from the manifest.
- **Code skills.** Skills with their own code, run in a sandbox and kept local to the machine. They are not for sharing.
- **Enforcement.** The daemon confining each skill to its declared permissions at run time.
- **A folder for user skills**, once the sandbox is there to hold them.
