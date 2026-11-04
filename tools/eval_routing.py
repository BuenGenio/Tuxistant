#!/usr/bin/python3
"""Which tool does the real model pick? Run this after adding a tool or changing a description.

    python3 tools/eval_routing.py [repeats] [text to filter on]

Nothing is executed: a turn stops at the first tool call. Needs the LocalAI server. The small
model is consistent, so a request that goes wrong usually goes wrong every time, and one tool's
wording can move requests that seem unrelated. Add a case whenever a real request goes wrong.
"""
import asyncio
import collections
import json
import sys
import time
from pathlib import Path

import websockets

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tuxistant.config import Config, DEFAULTS
from tuxistant.skills import Skills

cfg = Config()
KEY = cfg["api_key"]
skills = Skills(config=DEFAULTS)
REPS = int(sys.argv[1]) if len(sys.argv) > 1 else 3
ONLY = sys.argv[2] if len(sys.argv) > 2 else ""

# request, accepted tools (None: no tool is right)
CASES = [
    ("What's the weather like?", {"weather_forecast"}),
    ("What time is it?", {"time_now"}),
    ("How much disk space do I have?", {"disk_usage", "shell_run"}),
    ("What's the news?", {"news_headlines"}),
    ("Find my tax return for 2024", {"file_find"}),
    ("Where's that invoice I downloaded last week?", {"file_find"}),
    ("Open the PDF called annual report", {"file_find", "file_open"}),
    ("Send my CV to my phone", {"file_find", "phone_send", "task_run"}),
    ("Open my CV", {"file_find", "file_open"}),
    ("Find my tax return for 2024 and send it to my phone", {"task_run", "file_find"}),
    ("Where's my phone?", {"phone_ring"}),
    ("How much battery does my phone have?", {"phone_battery"}),
    ("Move everything in Downloads older than a year to the trash", {"file_trash"}),
    ("Clean up my Downloads folder", {"file_trash"}),
    ("Which folder in my home uses the most space?", {"disk_folders"}),
    ("How many files are in my Documents folder?", {"disk_folders"}),
    ("What's my IP address?", {"system_info"}),
    ("Is docker running?", {"system_running"}),
    ("Is Firefox running?", {"system_running"}),
    ("Install VLC", {"shell_run"}),
    ("Update my system", {"shell_run"}),
    ("Make a folder called Invoices in my Documents", {"shell_run"}),
    ("What does my todo file say?", {"file_find", "shell_run"}),
    ("How long has this computer been on?", {"system_info"}),
    ("How much memory am I using?", {"system_info"}),
    ("What's using all my CPU?", {"system_info"}),
    ("How much battery does this laptop have?", {"system_info", "phone_battery"}),
    ("How big is my Downloads folder?", {"disk_folders"}),
    ("What's in my Downloads folder?", {"shell_run", "file_find"}),
    ("Show me what's in my notes file", {"file_find", "shell_run", "file_open"}),
    ("How do you say good morning in Italian?", {"translate_text"}),
    ("What is the word for cheese in French?", {"translate_text"}),
    ("How do you say please in German?", {"translate_text"}),
    ("How do you say I love you in Russian?", {"translate_text"}),
    ("Translate where is the train station into Ukrainian", {"translate_text"}),
    ("How do you say thank you very much in Spanish?", {"translate_text"}),
    ("Translate what I've selected into French", {"translate_selection"}),
    ("What's the capital of France?", None),
    ("Tell me a joke", None),
    ("Remind me to call the dentist tomorrow", None),
]


async def first_tool(text):
    url = "ws://" + cfg["server"] + "/v1/realtime?model=" + cfg["pipeline"]
    async with websockets.connect(url, additional_headers={"Authorization": "Bearer " + KEY}, max_size=None) as ws:
        while json.loads(await ws.recv())["type"] != "session.created":
            pass
        await ws.send(json.dumps({"type": "session.update", "session": {"type": "realtime", "instructions": DEFAULTS["instructions"], "tools": skills.tools()}}))
        await ws.send(json.dumps({"type": "conversation.item.create", "item": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}}))
        await ws.send(json.dumps({"type": "response.create", "response": {"output_modalities": ["text"]}}))
        while True:
            e = json.loads(await asyncio.wait_for(ws.recv(), 90))
            if e["type"] == "response.function_call_arguments.done":
                return e["name"], e["arguments"]
            if e["type"] == "response.done":
                return None, ""
            if e["type"] == "error":
                return "ERROR", json.dumps(e.get("error"))[:100]


async def main():
    print(len(skills.tools()), "tools:", ", ".join(t["name"] for t in skills.tools()))
    print("tool list size:", len(json.dumps(skills.tools())), "characters")
    right = total = 0
    started = time.monotonic()
    for text, wanted in CASES:
        if ONLY and ONLY.lower() not in text.lower():
            continue
        seen = collections.Counter()
        sample = ""
        for _ in range(REPS):
            name, args = await first_tool(text)
            seen[name] += 1
            ok = (name is None) if wanted is None else name in wanted
            right += ok
            total += 1
            if name and name != "ERROR" and not sample:
                sample = args[:90]
        mark = "ok " if ((wanted is None and set(seen) == {None}) or (wanted and set(seen) <= wanted)) else "BAD"
        print(mark, text[:52].ljust(52), dict(seen), sample)
    print(f"{right}/{total} right, {(time.monotonic() - started) / max(total, 1):.1f} s each")

asyncio.run(main())
