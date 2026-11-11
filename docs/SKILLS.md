# Skills

A skill is something Tux can do for you, such as telling the time. It declares typed actions and the permissions it needs. The model sees the actions as tools and calls them during a conversation; the daemon runs them and hands the result back to be spoken.

## What exists today

Built-in skills, in `tuxistant/skills_builtin/`:

| Skill | Actions | Permissions | Risk |
|---|---|---|---|
| Time, Disk | `time.now`, `disk.usage` | none | low |
| This computer | `system.info`, `system.running` | `files_read` | medium |
| Folder sizes | `disk.folders` | `files_read` | medium |
| Weather, News | `weather.forecast`, `news.headlines` | `network` | medium |
| Files | `file.find`, `file.open` | `files_read` | medium |
| Trash | `file.trash` | `files_write` | high |
| Phone | `phone.ring`, `phone.battery` | none | low |
| Phone send | `phone.send` | `files_read` | medium |
| Lights | `lights.set`, `lights.status` | none | low |
| Calendar | `calendar.events` | `calendar_read` | medium |
| Add to calendar | `calendar.add` | `calendar_write` | medium |
| Mail | `mail.search` | `mail_read` | medium |
| Translate | `translate.text`, `translate.selection` | `network: [home]`, and `clipboard` for the selection | medium |
| General knowledge | `knowledge.answer` | `network` | medium |
| Web search | `web.search` | `network` | medium |
| Research | `research.run` | `network` | medium |
| Shell | `shell.run` | `shell` | high |
| Task | `task.run` | `shell` | high |

A skill with a `home` network permission only talks to the server this install already uses.

Calendar goes through Evolution Data Server, the service GNOME Calendar uses, so Google, CalDAV and Microsoft accounts are added once in GNOME's Online Accounts and Tuxistant holds no password or token for them. Reading needs no card, but it counts as reading something private, so afterwards nothing leaves your own server without a click. Adding always shows a card with the title, the full date, the times and the calendar. The model passes the day and time as you said them, and the dates are worked out in `tuxistant/calendars.py`, because a small model invents dates. An event's title is whatever the person who sent the invitation typed, so it is cut short and its control characters shown as escapes before the model sees it.

Mail answers a question from the search index Thunderbird keeps on this computer, so finding a mail needs no account, no password and no network, and the file is only ever opened read-only. Reading one uses the model on your own server, as everything does. It is off until you turn it on in Settings, and until then the model isn't offered the tool at all. Reading needs no card, but it counts as reading something private. A mail is a stranger's text, so none of it goes back to the voice model: the best matches are read one at a time by a model call with no tools, and the app says the answer itself, with who sent it and when added from the index. A sender is named by address or domain, never by the name beside it, which is whatever they typed. Junk, Trash and drafts are skipped. Sign-in codes and passwords are hidden from a mail before it is searched or read, so one can't be found or said, and a request for one is refused. Among mails holding as many of the question's words, one from an address that first wrote more than a week ago is read ahead of one from an address that didn't, and an answer from a first-time sender ends with a warning. [CONTRACT.md](CONTRACT.md) has the steps.

News declares every source Settings offers, and reads only the one you chose. The list is fixed in `tuxistant/livedata.py`, so a news request can't be pointed at a host that isn't written there, and a redirect to another host is refused. When a card has to ask before the news is fetched, it names the one host that will be reached.

Lights controls Tapo bulbs such as the L530 on your own network, so it needs no card, the same as ringing your phone. It finds them with a broadcast, then remembers them in `lights.json` beside the config and asks them directly too, since a bulb saving power misses some broadcasts. It uses the TP-Link email and password from Settings. The password never leaves this computer: KLAP only sends proofs that it is known, and the bulb has to prove it knows it first. Only typed values reach a bulb (on or off, a number, a colour from a fixed list), so it can't carry anything private out. A bulb's name is whatever anyone typed into the Tapo app, so it is cut short and its control characters shown as escapes before the model sees it. Settings lists each skill with its permissions and risk:

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
| `calendar_read` | Reading your calendars | medium |
| `calendar_write` | Adding to a calendar | medium |
| `mail_read` | Reading your mail | medium |

Set a permission to `true` to ask for it. Names not in this table have no effect on risk.

The permissions set the risk, show up in Settings and drive the rule below. They don't yet confine a handler, because the only handlers are the built-in ones. Confinement comes with the skill types below.

## What needs a click

`Skills.execute` is the only way an action runs, whether the voice model or the planner asked for it.

- **Anything risky.** A handler returns an `Ask` instead of acting. The card shows what will really happen, written by Tuxistant from the real action, and only a click on it goes ahead. A spoken yes doesn't count, because audio playing nearby could say it. No answer in 45 seconds, a cancel or a locked screen all mean no.
- **Private data leaving.** Once a conversation has read files, command output, the selection, your calendar or your mail, any action that reaches past your own server needs a click, whichever skill asks. One skill can't read your files and another send them out.
- **What runs with no click.** Reading, such as `file.find`, and shell commands the parser can prove are read-only. Those run in a sandbox with a read-only filesystem, no network and your secrets hidden. A web search, research or a fact looked up in a conversation that hasn't read anything private is also this kind of action, and needs no click.

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

## Facts

A question of general knowledge is answered from Wikipedia, then web search, and never from the model's memory. If neither clearly says, Tux says it doesn't know. On a hold turn the daemon decides this itself, as the model can't be made to. "Facts" in [CONTRACT.md](CONTRACT.md) has how. The question leaves your own server with no click, which is why `knowledge.yaml` declares `network: true`, and why "Look up facts on Wikipedia" in Settings turns it off.

## Web search and research

Searches go through SearXNG on your own server, set by `search_server`. Left empty, that is the host of the voice server on port 8888. The server is at home but the search isn't: your words go on to the search engines. So both skills declare `network: true`, not `home`, and once a conversation has read something private, a search needs a click like anything else leaving the house.

`web.search` returns at most four results, each a site, a title and a short snippet. `research.run` is ordinary code rather than a model deciding what to do next:

1. It searches, and takes the best result from each of up to five sites.
2. It opens them at once from this computer and keeps the first three that can be read. It never follows a link from a page.
3. The local model summarises each page, in a call that offers no tools.
4. The local model combines the summaries into two or three sentences, again with no tools.
5. Daemon code adds the sources, taken from the page addresses, and says so if a page failed, if there is only one source or if time ran out.
6. The app reads that out itself, as it does a translation. The voice model is told only that it's done, and isn't asked to speak after it. Given the text, it reworded it and dropped the sources and caveats almost every time, so a thin answer sounded certain. Given nothing and asked to speak, it sometimes made up an answer of its own just before the real one: a date, in 2 of 8 tries at "When did I apply to renew my passport?".

The whole thing stops at 45 seconds and answers with what it has. It always uses the local model, even with Cloud turned on. As the voice model never sees the answer, it can't take questions about it afterwards.

**Web text is untrusted.** All of it is cleaned and capped. Cleaning removes hidden characters and every angle bracket, as `<|im_start|>` would be a control token to the chat template and `<tool_call>` a real call to the server's parser. Search results reach the voice model under the key `untrusted_web_text`. Research never reaches it at all: only the summarising calls read pages, and they have no tools. So a hostile page can make Tux say something false out loud, such as a wrong phone number, but it can't act, and whatever the voice model does meets the same gates as always.

**Opening a page is guarded, as anyone on the web can choose the address.** `tuxistant/webfetch.py` takes http and https only, with no user name or password in the address. It looks up the name and refuses it unless every address it gets is on the open internet, which rules out loopback, private, link-local, multicast and reserved addresses, including ones wrapped in IPv6 and unusual spellings of numbers. It refuses the servers this install talks to, by name and by address. It connects to the address it checked rather than looking the name up again, and does all of this again for each of at most three redirects. A fetch stops after 8 seconds and 1 MB, reads only text and HTML, refuses compressed replies, and sends no cookies, no key and no referrer. It doesn't use a proxy.

## Multi-step requests

`task.run` handles requests that need several steps. It runs one step, reads the result and picks the next, with at most 8 steps, and every action goes through the same gates.

It uses the model on your own server unless you turn on Cloud in Settings, which is off by default and needs your Anthropic API key. With it on, Claude sees the request plus file names and command output from that task. File contents go only if you click to allow each one.

## Computed risk

`risk_of()` in `tuxistant/skills.py` works out the risk from the permissions:

- **high** if the skill asks for `shell` or `files_write`
- **medium** if it asks for `files_read`, `network`, `clipboard`, `calendar_read`, `calendar_write` or `mail_read`
- **low** otherwise

The risk is never read from the manifest. A `risk:` line is overwritten when the skill loads, so an author cannot label a dangerous skill as safe.

## Planned

None of this exists yet.

- **Recipe skills.** Declarative steps with no code of their own. These are the ones meant to be shared, because what they do can be read from the manifest.
- **Code skills.** Skills with their own code, run in a sandbox and kept local to the machine. They are not for sharing.
- **Enforcement.** The daemon confining each skill to its declared permissions at run time.
- **A folder for user skills**, once the sandbox is there to hold them.
