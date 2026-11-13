# Roadmap

Where the project stands and what comes next. Keep this current: it is how the next person picks up the work.

Last updated 2026-11-13.

## Where things stand

Steps 1 to 3 of the plan below are built, and most of step 4. `v0.2.0` is the latest stable release: it holds only what has worked on the owner's real desktop, plus what 0.1.0 already had. What has come since passes its unit tests and has been evaluated against the real server, with the results and the failures in [TESTING.md](TESTING.md), and most of it has **not yet run on the real desktop**: the list under "Check on the real desktop" says what has.


| Part | State |
|---|---|
| Daemon, blob, captions, Fn hold, status icon, Settings, themes | Working on the real desktop |
| Time, disk, weather, news | Working on the real desktop, by voice, with BBC News and The Kyiv Independent as the source |
| Approval gate and card | Working on the real desktop: cards for adding calendar events and running a command were shown and allowed on 2026-11-11. Deny and the lock-screen path not yet tried there |
| Files, phone, shell, system facts | Tested with the real model. See "Check on the real desktop" |
| Translation | Spoken translation works on the real desktop. Translating a selection has not been tried there |
| Calendar | Reads and adds events through Evolution Data Server. Tested against a real, private Evolution Data Server in two time zones, and reading was tried on the owner's ten Google calendars. On 2026-11-11 it read the calendar by voice and added four events through the card, to the local Personal calendar. Adding has not run on an online account, and CalDAV and Microsoft accounts have not been tried at all |
| Tapo lights | Worked on the real desktop with an L530 in 0.2.0, when it ran inside the daemon. Since it moved into the code skill sandbox it has not run on the real desktop (check 17). The bulb sometimes went missing until the Tapo app woke it. Since 2026-11-10 bulbs are remembered and asked directly. Over the next day it answered four of five times, and once said it couldn't find any lights |
| Mail search | Answers from the search index Thunderbird keeps, behind a switch that starts off. Tested against a made-up index, and with the real model on a made-up mailbox. Tried from a terminal on the owner's real mail on 2026-11-11, where it found and read the right mail in 1 to 3 seconds. In a trial that day it was asked four times through Tux on the owner's desktop: two answers, and two turns cut short. The full check below has not been done |
| Media control | Pauses, resumes and skips players through MPRIS, says what is playing, and sets the volume through `wpctl`. Tested against stand-in players, some of them on a private bus, and a stand-in `wpctl`. No real player has been told anything and no real `wpctl` has been run. Routing has been measured with the current tools; see [TESTING.md](TESTING.md) for results and limits |
| Timers and reminders | Wired into the daemon on 2026-11-11: `timer.set`, `timer.list` and `timer.cancel`, kept in `timers.json`, said through the controller without ever interrupting, with a notification. Tested with a clock that only tests move, on the real controller with fake audio, and through the real `service.run` on a private bus with a stand-in screen shield. Not yet in the real daemon, by voice or on the real desktop: no real notification has been shown and the chime has never been heard. Routing has been measured with the current tools; see [TESTING.md](TESTING.md) for results and limits |
| Home Assistant | Wired in on 2026-11-11, and offered to the model only once an address and a token are in Settings. Controls and reads lights, switches, fans, scenes, scripts, heating, covers, locks and media players by name. Tested against a fake server on localhost. Its author also ran it against a real Home Assistant 2026.11 in Docker with its demo devices, before a scene got its card: that test file is updated and has not been run since. Not yet on a real home, or by voice. Routing has been measured with the current tools; see [TESTING.md](TESTING.md) for results and limits |
| Apps and Settings pages | Built and tested with stand-ins. Not yet on the real desktop. Routing has been measured with the current tools; see [TESTING.md](TESTING.md) for results and limits |
| Dictation | Tested in an isolated real GNOME Shell with real windows, and against the real server. Not yet on the real desktop, with a real microphone, or in a real Electron app |
| Facts, web search, research | Tested against the real server, Wikipedia and SearXNG. Not yet by voice on the real desktop |
| Multi-step planner, local model | Two live runs worked |
| Multi-step planner, cloud | Tested against a stub only. Never called the real Claude API |
| Website | On GitHub Pages. See [website/README.md](../website/README.md) |
| Demo video | A 64 second draft from `tools/demo_video.py`: the real extension in an isolated shell, with scripted requests and synthesised voices, and it says so on screen. The owner accepted it on 2026-11-10. Not published anywhere yet |
| Server kit | In `server/`, copied from the owner's working server with its host name and user id made general. That general copy has not yet set up a second machine |
| Local code skills and their sandbox | Built and tested: your own skills load, change and go while the daemon runs, each in bubblewrap. Lights now runs this way. Not yet tried on the real desktop |
| Recipe skills, community registry | Not started |

The code is public under GPL-3.0.

## The plan

The owner approved this order.

1. **Done:** daemon, skill runtime, blob, status icon, Settings.
2. **Done, pending real-desktop checks:** typed skills, then the shell gate, with translation as the first real skill, and the planner with an opt-in cloud model.
3. **Done, pending real-desktop checks: dictation.** Tap Fn, then hold and speak, and the words are typed where the cursor is. No model is involved, so it needs no card. See "Dictation" in [CONTRACT.md](CONTRACT.md).
4. **Under way: more built-in skills.** Calendar is built. General web search, research, facts answered from Wikipedia, mail search for Thunderbird, media control, timers and reminders, and Home Assistant are done, pending real-desktop checks. Opening apps and Settings pages is built, pending real-desktop checks. Still to come: journal, and mail for people who don't use Thunderbird, through GNOME's Online Accounts.
5. **Local install of your own skills.** Recipe skills: YAML only, using built-in daemon actions (D-Bus, HTTP, open app), with no code of their own. They load from a user folder. Non-built-in skills must be refused `shell`.
6. **Built, pending real-desktop checks: code skill sandbox.** A skill with its own code runs as an MCP server in a bubblewrap sandbox with only the access it declared, and loads, changes and unloads while the daemon runs. See "Code skills" in [SKILLS.md](SKILLS.md). Local only. The only warning so far is a line on the card, "It is a skill you added.", shown when the taint rule asks.
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
5. **What "journal" means**: a spoken diary saved as dated files, notes on calendar days, or a particular app. Calendar can use Evolution Data Server, so it follows the accounts in GNOME Online Accounts, and any change needs a card.

### Choices made while building, worth a look

- With Cloud on, a conversation that has already read something private gets a "Send this request to Claude?" card before the task starts, because the voice model could paste file text into the goal. Every task sets that state, so a second task in one conversation always shows it. `tuxistant/planner.py`, about 3 lines to remove.
- Cloud uses `claude-opus-5-5` at low effort, because someone is waiting in silence, with server-side refusal fallbacks on. `CLOUD_EFFORT` is one constant.
- The consent dialog shows every time Cloud goes from off to on, not literally once. A single-time prompt needs a new config key.
- `file.open` refuses HTML, SVG, archives and disk images as well as anything that runs code.
- Translations of more than 2000 characters are refused, not cut.
- Timers: the chime is off as installed (`timer_chime`), so that nothing sounds on a locked screen unless you ask for it. The same switch decides whether a chime comes before the words on an unlocked screen. Two switches would need a second key.
- Timers: no card for any of the three actions, even in a conversation that has already read something private. A model talked into it by a hostile file can therefore set a reminder whose words Tux says later, as "Reminder: ...". A card for `timer.set` in such a conversation would close that, at the cost of a click for "find my tax return and remind me tomorrow to send it".
- Timers: listing or cancelling counts as reading something private because the handler says so, not the manifest, so the Skills page shows no permission for it. The other way is a second manifest with a new `reminders_read` permission.
- Home Assistant: activating a scene shows a card, like running a script, because a scene can unlock a door or open a garage and the skill can't see what is in one. "Movie time" every evening therefore costs a click. A switch or a helper has a card only when its name or id says alarm, siren, lock, security, arm, disarm or deadbolt, joined spellings such as Doorlock included, or garage, door or gate without light, lamp, bulb, heater or fan, though any of them can be wired to anything. `ASKS` in `tuxistant/homeassistant.py`, one entry.
- Home Assistant: reading the state of the house counts as reading something private (`home_read` in `reads_private`). The cost is that after "Is the front door locked?", weather, news or a web search in the same conversation shows a card. Both Home Assistant handlers also mark the conversation after reading the device list, including when a change or a failed match reveals names.
- Home Assistant: its two tools are left off the model's list until both an address and a token are set, so an install without one is as it was. With both Home Assistant and Tapo set up the list is two longer; the Tapo tools are offered only once a TP-Link email and password are set, whether or not Home Assistant is. The combined lists have been measured; results and remaining misses are in [TESTING.md](TESTING.md).
- Home Assistant: Settings has no Test button and `tuxistant doctor` has no line for it, so the first sign of a wrong address or token is what Tux says when asked. `homeassistant.check` exists for both and is not called from anywhere.
- Apps: no card for an app installed as a package or for a Settings page, even after the conversation has read a web page or a mail. The model only picks which entry from the installed list, nothing goes with it, and opening sends nothing out, so the worst is an unwanted window. A terminal is included, as Tux can't type into it. A card for every app after private data would close the gap for packaged apps that act as they start, at a click for "find my CV, then open Files".
- Apps: an entry whose real file is in the home folder or anywhere else outside a package, such as `/opt`, shows a card with the command it runs, as that command can be anything and its name may come from a web page. A shortcut made on purpose, a Steam game or a "userapp" default browser costs a click every time. `_start` in `tuxistant/apps.py`.
- Apps: each app starts in a systemd scope of its own through `systemd-run --user --scope`, so it isn't in `tuxistant.service`, under its 1 GB memory limit, or stopped by a daemon restart. Without `systemd-run` it starts as the daemon's child. `file.open`'s `gio open` still has that problem for apps that aren't started over D-Bus.
- The installer now enables the user unit for everyone, not only with the Fn helper, so the daemon starts at every login. `install.sh`, one line.

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
10. Hold Fn and ask a fact, such as "How many chemical elements are there?". It should answer in the usual voice within a few seconds, saying "Let me look into that." first if it takes more than 2, and History should show `knowledge.answer`. Then something made up, such as "What is aubrium?", which should say it doesn't know.
11. Hold Fn and ask for something that isn't a fact, such as "What time is it?" and "Find my CV". Replies now come from words rather than audio, which has only run in tests and against the real server without a real microphone.
12. "Research electric bikes" says "Let me look into that", then reads the answer with its sources. After reading a file in the same conversation, a search or a fact shows a card first.
13. Dictation, with `tuxistant doctor` saying it's ready: tap Fn, then hold and speak, in a GTK app such as Text Editor, in a terminal, and in VS Code or Chromium, which should take the paste route. Check that the clipboard is unchanged afterwards, that a password box refuses, and that Ctrl+Super+D starts and stops it.
14. **Passed 2026-11-10:** lights, with the TP-Link account in Settings: "Is the light on?", then "Turn off the light" and "Make it warm white".
15. **Passed in part 2026-11-11:** read by voice, and four events added through the card to the local Personal calendar. Adding has not yet written to an online account. Calendar: "What's on my calendar tomorrow?" by voice, then "Add a test event on Friday at 3pm", allow the card, and see it appear in GNOME Calendar and on the account. Pick a calendar in Settings first.
16. Mail search, with Thunderbird set up: turn on "Search my mail" in Settings and ask about a mail you remember, such as when something was booked or what someone said. It should answer within a few seconds and say who it was from and when. Ask for a sign-in code, which it should refuse. Ask about a mail you know holds a code, such as what a sign-in mail from your bank said, and the code should come out as "hidden". After an answer from mail, ask for a web search in the same conversation, which should show a card. `tuxistant doctor` says whether it found any mail.
17. Lights again, now that they run sandboxed: the same three requests. This checks that the broadcast, the direct asks and `lights.json` all work from inside bubblewrap, under Landlock. Restart the daemon first, so this also checks that the light tools are still offered after a restart with the TP-Link account in Settings.
18. Copy a small skill into `~/.local/share/tuxistant/skills/`, ask for it, edit it, ask again, then delete it, all without a restart. `tuxistant doctor` should show `skill sandbox ready`.
19. Media, with music playing in Spotify or a video in a browser: "What's playing?", "Pause the music", "Resume the music", "Skip this song", then "Turn it down", "Volume 40", "Mute" and "Unmute". Watch the volume slider: it must follow and never pass 100 percent. Start a second player and say "Pause the music": it should ask which. On an Fn hold, "Who sings this?" should show `media.playing` in History, not `knowledge.answer`. No real player and no real `wpctl` has ever been driven: every test uses stand-ins.
20. Timers. "Set a timer for one minute": a minute later "Your 1 minute timer is done", the caption, and a notification that stays in the list. "Set a pasta timer for two minutes", then lock the screen: no sound and no words while locked, the lock screen lists a notification without its text, and on unlock "Your pasta timer finished ... ago". "Remind me in one minute to stretch", then hold Fn and keep talking past the minute: nothing cuts in, and it is said after you let go. Restart the daemon with one pending and it still comes. Log out and in with a reminder due meanwhile: it is said late, about 15 seconds after login. Suspend through a timer: it is said within half a minute of waking. Switch on "Chime for timers" in Settings and do the first two again: a chime before the words, and on the locked screen the chime alone. With the chime still on and the speech server stopped, let a timer come due: the chime is heard in full and one error shows. Start the server within a minute: the words come about a minute later, as late words with no chime. On an Fn hold, "How long is left on the pasta?" should show `timer.list` in History, not `knowledge.answer`. After `./install.sh --no-fn-helper` and a login, `systemctl --user is-enabled tuxistant` should say enabled. None of this has run in the real daemon.
21. Home Assistant, with the address and a long-lived access token in Settings, address first. The token row should say "Token is set" and never show it. Then by voice "Is the front door locked?", "Turn off the kitchen light" with the room being one of yours, "Set the heating to 20", and "Unlock the front door": a card names the lock, its entity id and your server, Deny leaves it locked, Allow unlocks it. Activate a scene: a card too. After the question about the door, ask for the weather in the same conversation: a card. Then "Turn on the light" and see that the Tapo lamp still answers. On an Fn hold, "Is the front door locked?" should show `home.status` in History, not `knowledge.answer`. Change the address in Settings and see the token go. None of this has run on a real home.

22. Apps: "Open Firefox", "Open the Bluetooth settings" and "Open Settings". Then restart the daemon and check that Firefox stays open. Make a test shortcut in `~/.local/share/applications`, ask for it, and see its card. Check whether the new window gets focus or GNOME shows a "ready" notice instead. None of this has run on the real desktop.

Move site claims from Soon to Now only after the matching check passes.

## Known limits

**The 4B model**, measured with `tools/eval_routing.py`:

- "Update my system" and "Make a folder called Invoices" don't reach the shell. It says it can't, or picks the wrong tool.
- It sometimes opens a file on the way to sending it, which is harmless but not asked for.
- After a declined card it may say "I can't" rather than "OK, I've left that alone".
- It chains `file.find` then `phone.send` itself and rarely picks `task.run`.
- Its answers about the weather can pair days oddly, and "do I need a coat" varies.
- With its tools in front of it, it refuses some plain facts and makes up others, such as "aubrium" for the rarest element. Telling it to look things up doesn't work: it picks the tool about one time in seven, and stronger wording broke "Where's my phone?". That's why hold turns are sorted before it hears them. A turn from the shortcut isn't, and still gets this model. See "Facts" below.
- On a turn that does reach it, every tool costs plain facts. Adding the two light tools: 43 of 50 answered without them, 35 to 40 of 50 with them (5 repeats, 2026-11-10). "When did World War Two end?" went from 5 of 5 to 0 of 5. Shorter wording and a single tool didn't help, as the refusals only moved to other questions. All 27 light requests route right.
- About 1 fact in 80 comes back confidently wrong, such as Charlotte Brontë for Pride and Prejudice, and "What language do they speak in Brazil?" can reach the translate tool.

**Facts:**

- Only hold turns are sorted. A turn from the shortcut goes straight to the voice model, as the server replies by itself, so those can still be made up.
- The sorter runs on every hold turn, which adds about 0.3 s to every request, and a stall can add up to 2 s.
- A fact takes 2 to 5 seconds from letting go, where other requests take under 2. Wikipedia is most of that, at 0.5 to 3.5 s.
- A question goes to Wikipedia with no click, on the word of a 4B model. Every word 0.3.0 kept from the sorter, such as my, our, thermostat, timer, locked or front door, still keeps a hold turn from it. On any turn the voice model answers (a turn from the shortcut, or a hold turn not sorted as a fact) it can give the lookup tool such a question itself: the tool refuses the ones whose shape is surely this home's, and the rest get a card first. A private request with none of those words, such as "Who's at the door?", can still be called a fact on a hold turn, or be given to the lookup by the voice model on any turn it answers, and then the words leave with no click. The other way round, a fact with one of those words, such as "How old is our galaxy?", isn't sorted on a hold turn, so it is looked up only if the voice model picks the tool and you click. Where the shapes can't tell a fact from this home, the tool refuses, so a few facts are never looked up, such as "Is the Large Hadron Collider still running?". The last run with the model, at commit 79b75de and after the filter was split in two, called nothing else a fact in 552 tries ([TESTING.md](TESTING.md)), and those are wordings someone could think of.
- Web search answers from snippets of other people's text, so a false page can make Tux say something false, out loud. It can't do anything.
- It says what pages say, and doesn't know what's fiction. "The capital of Zubrowka" gets Lutz from a fan wiki, with no mention that it's from a film. "Aubrium" gets an Instagram tool of that name.
- Wikipedia is the English one. A question in another language may find less.
- Follow-up questions aren't understood: each hold is a new conversation, and "Who wrote it?" means nothing to a search.
- The voice model never sees an answer the app read out, so it can't take questions about it.

**Mail search:**

- It reads Thunderbird's index and nothing else, so it knows what Thunderbird knew when it last ran, and a mail Thunderbird hasn't downloaded has no text to search. Other mail programs aren't read at all.
- The index is Thunderbird's own, and its layout could change with a new version. Then mail search says it couldn't read it, until this is updated.
- It matches words, not meanings. "When did I pay the plumber?" won't find a mail that only says "invoice settled", and a question in one language won't find a mail in another.
- A phrase such as "last week" is not understood: newer mail is preferred, and that is all.
- When nothing in the mailbox is about the question, the three nearest mails are still shown to the model before it says so. That takes about 4 seconds, and those mails go to your own server like any other.
- On the owner's index of 130,000 mails the search itself took 0.5 to 2.5 seconds, before the model reads anything.
- Who a mail is from is the address the mail gives. Nothing here checks a signature, so a forged address that reached the inbox is believed, and an address that has written for more than a week counts as known however it began. A company that writes from a new address each time gets the first-time warning each time, and so does a sender line that isn't a plain name and address, or whose address is at a domain not written in ASCII.
- When a sender first wrote is worked out from the mails Thunderbird indexed just after their first one. Of 239 senders on the owner's index, 238 came out the same as by that mail's own date, and one long-standing sender was taken as new. So the warning can be wrong now and then, mostly by being said when it needn't be. The day that is said is the one the mail gives.
- A mail can still make Tux say something false, out loud. It can't make it do anything. The warning for a first-time sender is the only sign.
- Sign-in codes and passwords are hidden by the words around them and by their shape. One that fits neither would be read out, such as a password given as "the usual one plus 42", or a code more than 160 characters after the word for it. The other way round, too much is hidden: a promo code, a booking reference, an order number in a mail that also says something expires. The answer then has a gap, read out as "hidden". Of the newest 2,000 mails on the owner's index, 253 had something hidden.
- Asking for "the code the bank sent" without saying sign-in, verification, password or PIN is not refused outright. The search runs, the mail may be found, and the code in it is hidden.
- Only the best three mails are read. If three from known senders hold as many of the words, a first-time sender's mail that would have answered is never reached.
- The small words of languages other than English, Russian and Ukrainian are searched for like any other, so a German or French question finds more noise.
- Junk and Trash are known by Thunderbird's own mark or by name. A junk folder Thunderbird hasn't marked, named in a language the list doesn't have, would be read.
- An answer in another language is shown, not spoken.
- With it switched on, the tool list is one longer. Two runs with it answered 27 and 28 of 33 plain facts on shortcut turns, and one without it answered 31, so it may cost a few. Hold turns aren't affected, and neither is anyone who leaves it off.
- "When is my dentist appointment?" can go to the calendar instead, which is a fair reading of it. Saying "email" settles it.
- In a multi-step task that both asks a question the app answers and does something else, the something else is summed up as "The other steps are done.", not in the model's words.

**Media:**

- No real player has been told anything and no real `wpctl` has been run, to read the volume or to change it. What `wpctl` takes and prints was read from its help and from the binary here (WirePlumber 0.5.13). How long a real player needs to show a change is a guess: Tux waits up to 1.5 s and then says what it sees.
- Before `app_open`, "Open Spotify" and "Play Spotify" arrived as a bare play with no name, which starts whatever was left paused, such as a video in a browser. Media control can't open an app. "Open Spotify" now goes to `app_open` 3 of 3 in every setup measured, but "Play Spotify" is still a bare play.
- Other requests the model gets wrong: "Next" alone goes to the calendar, "Resume" alone gets "I can't resume anything", "Unpause" comes as unmute, "Half volume" comes as volume down, which is a step down unless the model put 50 beside it, and "Pause the music and turn the volume down" does one of the two. "Next song" and "Resume the music" are right.
- Tux can't tell you the volume without changing it.
- After "Mute", Tux's own voice is muted too, so the answer is only seen in the caption.
- Only the default output is touched, and it is never set above 100 percent. Not a second output, not one app's own volume, and never the microphone.
- "Down by 20" and "down to 20" reach the handler as the same thing. It takes whichever moves the volume less, so one of the two is always a little off.
- "Stop the music" pauses, so it can be resumed.
- With two players playing and nothing to say which started last, Tux asks which you mean. Only playerctld knows the order, and it isn't on a GNOME desktop by default. Its interface was read from its source and tried against a stand-in, never the real thing. Two windows of one app playing the same title count as one.
- A player that doesn't speak MPRIS can't be seen: mpv without its plugin, or Firefox with media keys switched off. A phone's players that GSConnect mirrors onto the bus are players like any other.
- A title with right-to-left marks or joined emoji is read out with its escapes in it.
- Media replies, including player names in errors, are read by the app itself. They never enter the model context.
- The two tools were measured with the real model on 2026-11-11 on an earlier list of 21: the same 171 of 177 requests routed right with and without them, and 29 and 31 of 33 plain facts were answered. The current lists, 30 to 32 tools in the four setups in [TESTING.md](TESTING.md), were measured on 2026-11-13; see there for the results. A sorter check on 73 new requests, including "Who sings this?", returned no false facts.

**Apps:**

- It can't switch to or raise an app that is already running, as GNOME Shell lets only itself do that. A running Firefox opens a new window.
- There is no closing. "Close Firefox" opened Firefox until `app_open` said it can't close apps.
- "Launch the calculator" runs the shell command `calc` in two of the four setups measured, and "Open my Downloads folder" reaches `app_open`, which says no app has that name and offers to look for a folder.
- A window opened from the daemon may not take focus.
- Settings words are English. A page's translated name matches only when said exactly.
- A step in a multi-step task that opens an app ends the task, as media does.
- A default browser set with "Open with a custom command" is a home-folder entry, so it gets a card.
- When the same app is installed twice, Tux asks "Flatpak Firefox or system Firefox?", and the answer has to name the kind again. The model never sees the choices, so "the first one" doesn't work.

**Timers and reminders:**

- None of it has run in the real daemon, by voice or on the real desktop. No real notification has been shown and the chime has never been heard. That GNOME lists the notification on the lock screen with a count and no text, lights the display and plays no sound there is from reading Shell 50.1's code, not from seeing it.
- Routing and plain facts have been measured with all three timer tools; see [TESTING.md](TESTING.md). The sorter check included "How long is left on the pasta?" and returned no false facts among the 73 new requests tested. Unsupported scheduling and cancellation requests can still choose a tool; the tool list is not an intent guarantee.
- The computer has to be on and you logged in. A timer can't wake a suspended laptop, and nothing is said while logged out. What came due meanwhile is said late: a timer up to an hour after, a reminder up to a day after. Older ones only get a quiet notification.
- On a locked screen there is a notification and no sound, and the words come if you unlock within an hour for a timer, or a day for a reminder. After that only the notification remains. GNOME locks after five idle minutes by default, so a ten-minute kitchen timer usually ends in silence unless "Chime for timers" is on.
- A timer waits for a turn, a card or dictation to end before it is said, which can be minutes for a long dictation. The notification is on time.
- It is said once. Nothing rings until dismissed, and there is no snooze. Anything that stops Tux speaking, such as the shortcut, cuts it short, and once some of it was heard it counts as said. A screen lock that cuts it before any word is heard does not count: it is said after the unlock.
- The words need the server. With the server off there is the notification and an error. A chime, if it is on, finishes even if fetching the words fails. Words that fail before any is heard are tried again after one, two and four minutes, with an error each time, and only then recorded as missed.
- No repeating reminders, no changing one, and "in a month" isn't understood: days and weeks are.
- A timer follows the system clock, so a clock that is corrected while one runs moves it. After a change of time zone a reminder keeps its moment, not its clock time.
- "Wake me at 7" is 7 in the morning only if the model passes "7am" or says it is to wake you. Otherwise a bare "at 7" is the next time the clock shows 7, so said mid-morning it is 7 in the evening. The reply always names the time.
- "Remind me to call the dentist tomorrow" may become a reminder at 9 in the morning or a calendar entry with a card, whichever tool the model picks.
- With one timer set and nothing named, "stop" or "cancel" may reach `timer.cancel` and cancel it. It says which one it cancelled.
- A reminder's label is said aloud to whoever is in the room, and shown in a notification.
- Listing or cancelling marks the conversation as having read something private, so weather, news or a web search later in the same conversation asks for a click. Each Fn hold is a new conversation.
- At most 50 pending. `timers.json` is plain text, readable only by you, and clearing the history doesn't clear it.
- Two unnamed timers of the same length can't be told apart by voice: "cancel the 10 minute timer" lists both and cancels neither, and "all" cancels both.
- Do Not Disturb hides the banner. Tux still speaks.
- Nothing lists timers on screen: the panel menu and Settings don't show them, and there is no D-Bus call for it yet.
- A daemon that crashes or is stopped mid-sentence says the reminder again at its next start.

**Home Assistant:**

- It has never run against a real home or by voice. Which tool the model picks with Home Assistant, with and without the Tapo lights, was measured on 2026-11-13; [TESTING.md](TESTING.md) has the commands, the results and the misses. Words about the home, the music playing and timers, such as locked, lights on, front door, the heating, this song, time left, my or our, now go the normal way before the sorter is asked, so the sorter never sends those to Wikipedia. On any turn the voice model answers (a turn from the shortcut, or a hold turn not sorted as a fact) it can still pick `knowledge_answer` for one, but the tool refuses those in a shape that is surely this home's, and a card comes before it sends the rest. A home question worded without any of them, such as "Who's at the door?", still depends on the sorter, whose prompt now names them, and on any turn the voice model answers it can still reach Wikipedia through that tool, with no card.
- With both this and Tapo lights set up, "turn on the light" should stay with the Tapo skill and a named light or a room come here. Nothing makes up for the model choosing the other one. Sent to the wrong tool, a named light is not found, with one exception: with a single Tapo bulb, the Tapo skill takes any name to mean that bulb, so "turn on the kitchen light" sent there switches it.
- Areas come from Home Assistant's template engine, which only an administrator's token may use. With another user's token, things are found by name only. Floors and labels are not used.
- A switch or a helper runs with no card unless its name or id says alarm, siren, lock, unlock, security, arm, armed, disarm, disarmed, arming, disarming or deadbolt, joined spellings such as Doorlock included, or garage, door or gate without also saying light, lamp, bulb, heater or fan: those wait for a click whichever way they are switched. One wired to a door but named for something else, such as "Relay 2", still runs with no card, and a helper can still trigger an automation that unlocks one.
- The Tapo tools are offered only once Settings holds both a TP-Link email and password, as Home Assistant's are with an address and a token. On a fresh install neither is offered, and "turn on the light" gets no light tool until one is set up.
- Opening any cover needs a click, a blind as much as a garage door, as device classes are too often missing to tell them apart. So does every scene, however harmless.
- No fan speed, cover position or stop, media playback or volume, heating mode, or a range for a thermostat that holds one. A thermostat holding a range refuses a single temperature.
- An alarm panel is read and never armed or disarmed. People, device trackers, cameras and anything else not listed are neither read nor changed.
- A state that isn't a number or one lowercase word is not read out, which leaves out timestamps such as the next sunrise, and menus whose options are written as prose.
- More than twelve things at once are read from one list a few seconds old, and from a fresh one after a change. A thing added in Home Assistant can take a minute to be found while another thing matches the same words.
- Plain http is only used on a private address, and goes to the first one the name has, IPv4 first. A server that answers only on another of its addresses is not reached, and one on the internet needs https.
- With https the certificate must be one this computer trusts. A self-signed one is refused, and there is no setting to accept it.
- Replies over 16 MB for the list of everything, or 1 MB for anything else, are refused.
- Names are matched in English: "light", "lamp", "door" and the other kind words are English.
- Asking about the house marks the conversation as having read something private, so weather, news or a web search later in the same conversation asks for a click. Each Fn hold is a new conversation.
- The address has to be saved before the token: a new address clears the token, and Settings says so.
- Nothing checks the address and token when they are entered. A wrong one is only found when you ask for something.
- The colour and brightness words are a copy of the Tapo skill's, in `tuxistant/colours.py`, so that this skill stands without it. A test fails if the two drift apart while both are here.

**Web search:**

- A page address can't reach your network or your servers, but it can reach this computer's own public address, which it could only use to send a blind request.
- SearXNG depends on search engines that block automated use. On this server DuckDuckGo, Google and Qwant refuse it, and only Brave and Bing answer. A block can last for hours.

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
- The calendar can read and add, and nothing else: no changing, moving or deleting an event, no invitations, no locations and no alerts. "Remind me to call the dentist tomorrow" may become an all-day calendar entry with a card, or a spoken reminder at 9am; see the timer limits above.
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
- **Done 2026-11-13:** `v0.3.0` is tagged as a preview (a GitHub prerelease): web search and facts, mail search, your own skills, media, timers and Home Assistant, after four rounds of fixes and review. Only mail search has been asked on the real desktop. The site's download stays on 0.2.0 until the new parts have been seen working there.
- Have native speakers check the site's translations, including the Auto/Light/Dark words. Five languages now, 25 to 30 later.
- Replace the favicon, which is still the old penguin.
