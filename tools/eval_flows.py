#!/usr/bin/python3
"""Whole requests against the real model with the real tools: what it calls, what the cards say
and what Tux would say.

    python3 tools/eval_flows.py ["a request" ...]

Every card is declined. That alone would not make this safe, as actions with no card really run:
it once opened the user's CV on screen. So opening files and phone actions are cut off below, and
any new action with no card that touches the desktop or a device must be cut off here too. What
still runs: file search, reading system facts, sandboxed read-only commands, weather, news,
translation on your own server, and web search and research, which reach real search engines and
real pages.
"""
import asyncio
import json
import sys
import time
from pathlib import Path

import websockets

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tuxistant import files, phone  # noqa: E402
from tuxistant.config import Config  # noqa: E402
from tuxistant.gate import Session  # noqa: E402
from tuxistant.skills import Skills  # noqa: E402


def blocked(*_args):
    raise RuntimeError("Blocked by eval_flows")


files._launch = lambda path: print(f"   (would open {path.name})") or True
phone._activate = blocked

cfg = Config()
KEY = cfg["api_key"]
skills = Skills(config=cfg)
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
    "Install VLC",
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
        reply, called, rounds = "", False, 0
        while True:
            e = json.loads(await asyncio.wait_for(ws.recv(), 120))
            kind = e["type"]
            if kind == "response.function_call_arguments.done":
                _, result = await skills.execute(e["name"], e["arguments"], session)
                calls.append(f'{e["name"]}({e["arguments"][:80]}) -> {json.dumps(result, ensure_ascii=False)[:150]}')
                called = True
                await ws.send(json.dumps({"type": "conversation.item.create", "item": {"type": "function_call_output", "call_id": e["call_id"], "output": json.dumps(result)}}))
            elif kind.endswith("transcript.done") or kind == "response.output_text.done":
                reply = e.get("transcript") or e.get("text") or reply
            elif kind == "error":
                return calls, cards, session, "ERROR " + json.dumps(e.get("error"))[:120], time.monotonic() - started
            elif kind == "response.done":
                if called and rounds < 5:
                    called, rounds = False, rounds + 1
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
