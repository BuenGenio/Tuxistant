#!/usr/bin/python3
"""Whole requests against the real model with the real tools: what it calls, what the cards say
and what Tux would say.

    python3 tools/eval_flows.py [--server host:port] ["a request" ...]

Every card is declined, and every way out an action with no card has is replaced by
tools/eval_cutoffs.py, which tests/test_realtime_live.py uses too: any new action must be named
there. Code skills, the lights among them, are offered but never started. What still runs: file
search, reading system facts, sandboxed read-only commands, weather, news, translation on your
own server, and web search and research, which reach real search engines and real pages. Mail
search, media, timers and Home Assistant run on made-up ones: a mailbox, a player and a volume,
a store in a temporary folder that nothing announces from, and a home no request leaves for.

The stored key goes only to a server named for this run (TUXISTANT_SERVER or --server) or saved
in Settings and not the default: see tools/eval_config.py.
"""
import asyncio
import json
import sys
import time
from pathlib import Path

import websockets

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tools.eval_config import evaluation_server  # noqa: E402
from tools.eval_cutoffs import install  # noqa: E402
from tuxistant.config import Config  # noqa: E402
from tuxistant.gate import APP_SAYS, Session  # noqa: E402
from tuxistant.skillhost import offline  # noqa: E402

# Before anything else, and never undone: the evaluation ends with the process.
# tests/test_eval_config.py checks that this comes first and that nothing here changes it.
CUT_OFF = install()

cfg = Config()
evaluation_server(cfg)
cfg.values["mail_search"] = True    # here only: nothing is saved
# Its tools are offered only when it is set up, and they are to be evaluated whether the owner
# has one or not. Made-up values, so the real address and token are not in what Skills is given.
cfg.values["ha_server"], cfg.values["ha_token"] = "eval.invalid:8123", "eval"
cfg.values["tapo_email"], cfg.values["tapo_password"] = "eval@example.test", "eval"   # the same
KEY = cfg["api_key"]
skills = offline(cfg).current()     # no code skill runs, so no light is reached
ASKS = sys.argv[1:] or [
    "Find my tax return for 2024 and send it to my phone",
    "Find my CV and send it to my phone",
    "Clean up my Downloads folder",
    "Move everything in Downloads older than a year to the trash",
    "Which folder in my home uses the most space?",
    "How many files are in my Documents folder?",
    "What's my IP address?",
    "How long has this computer been on?",
    "Is docker running?",
    "Translate where is the train station into Ukrainian",
    "How do you say thank you very much in Spanish?",
    "Search the web for the latest Ubuntu release",
    "What's the price of bitcoin?",
    "Research electric bikes for me",
    "How many files are in my Documents folder? Then search the web for that number",
    "Delete my old log files",
    "What's on my calendar tomorrow?",
    "Add dentist on Friday at 3pm to my calendar",
    "When did I apply to renew my passport?",
    "What did my landlord say about the rent?",
    "Did the bank email me about my new card?",
    "Was my application to renew my passport rejected?",
    "Check my email for the dentist appointment, then search the web for the dentist's reviews",
    "Install VLC",
    "Pause the music", "What's playing?", "Skip this song", "Turn it up",
    "Set the volume to 40 percent", "Mute",
    "Set a pasta timer for 8 minutes", "Remind me at 5pm to call mum",
    "Remind me tomorrow morning to take the bins out", "How long is left on the pasta?",
    "Cancel my timers",
    "Turn off the kitchen light", "Is the front door locked?", "Set the heating to 21",
    "Unlock the front door", "Open the garage", "Activate movie time",
]

async def turn(text):
    url = "ws://" + cfg["server"] + "/v1/realtime?model=" + cfg["pipeline"]
    cards, calls, started = [], [], time.monotonic()
    async def decline(ask):
        cards.append((ask.level, ask.title, ask.detail[:120].replace("\n", " | ")))
        return False
    session = Session(skills, decline)
    async with websockets.connect(url, additional_headers={"Authorization": "Bearer " + KEY}, max_size=None) as ws:
        while json.loads(await ws.recv())["type"] != "session.created":
            pass
        await ws.send(json.dumps({"type": "session.update", "session": {"type": "realtime", "instructions": cfg["instructions"], "tools": skills.tools()}}))
        await ws.send(json.dumps({"type": "conversation.item.create", "item": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}}))
        await ws.send(json.dumps({"type": "response.create", "response": {"output_modalities": ["text"]}}))
        reply, called, report, rounds = "", False, False, 0
        while True:
            e = json.loads(await asyncio.wait_for(ws.recv(), 120))
            kind = e["type"]
            if kind == "response.function_call_arguments.done":
                _, result = await skills.execute(e["name"], e["arguments"], session)
                calls.append(f'{e["name"]}({e["arguments"][:80]}) -> {json.dumps(result, ensure_ascii=False)[:150]}')
                called = True
                report = report or not result.get(APP_SAYS)     # as Realtime._run_tool has it
                await ws.send(json.dumps({"type": "conversation.item.create", "item": {"type": "function_call_output", "call_id": e["call_id"], "output": json.dumps(result)}}))
            elif kind.endswith("transcript.done") or kind == "response.output_text.done":
                reply = e.get("transcript") or e.get("text") or reply
            elif kind == "error":
                return calls, cards, session, "ERROR " + json.dumps(e.get("error"))[:120], time.monotonic() - started
            elif kind == "response.done":
                if called and report and rounds < 5:
                    called, report, rounds = False, False, rounds + 1
                    await ws.send(json.dumps({"type": "response.create", "response": {"output_modalities": ["text"]}}))
                else:
                    return calls, cards, session, reply, time.monotonic() - started

async def main():
    for text in ASKS:
        calls, cards, session, reply, took = await turn(text)
        print(f"\n>>> {text}   [{took:.1f} s]")
        for c in calls: print("   call:", c)
        for c in cards: print("   CARD:", c)
        for s in session.take_after(): print("   app says:", s.text, "| voice:", s.model or s.voice or "(shown only)")
        print("   Tux:", reply[:260])

asyncio.run(main())
