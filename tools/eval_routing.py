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
import re
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
    ("Turn on the light", {"lights_set"}),
    ("Turn off the lights", {"lights_set"}),
    ("Switch off the bedroom lamp", {"lights_set"}),
    ("Dim the light to 30 percent", {"lights_set"}),
    ("Make the light brighter", {"lights_set"}),
    ("Make the lights blue", {"lights_set"}),
    ("Set the light to warm white", {"lights_set"}),
    ("Is the light on?", {"lights_status"}),
    ("What colour is the lamp?", {"lights_status"}),
    ("What's on my calendar today?", {"calendar_events"}),
    ("Do I have any meetings tomorrow?", {"calendar_events"}),
    ("When is my next appointment?", {"calendar_events"}),
    ("Am I free on Friday?", {"calendar_events"}),
    ("What's my schedule for this week?", {"calendar_events"}),
    ("Add dentist tomorrow at 3pm to my calendar", {"calendar_add"}),
    ("Schedule a meeting with Anna on Friday at 10", {"calendar_add"}),
    ("Put lunch with Sam in my calendar for the 14th of November at noon", {"calendar_add"}),
    ("Book the car service for next Monday", {"calendar_add"}),
    ("What's the capital of France?", None),
    ("Tell me a joke", None),
    # A calendar entry, with a card. Not an alert, which nothing here can set.
    ("Remind me to call the dentist tomorrow", {"calendar_add"}),
    ("Send an email to Anna", None),
]

# Questions it should simply answer. "No tool" alone can't tell an answer from "I can't do that".
FACTS = [
    ("How many chemical elements are there?", r"118|hundred and eighteen"),
    ("Who wrote Pride and Prejudice?", r"austen"),
    ("How many bones are in the human body?", r"206"),
    ("What's the speed of light?", r"299|300|186"),
    ("What's the square root of 144?", r"\b12\b|twelve"),
    ("What's 15 percent of 80?", r"\b12\b|twelve"),
    ("When did World War Two end?", r"1945"),
    ("What year did the Titanic sink?", r"1912"),
    ("What is the tallest mountain in the world?", r"everest"),
    ("Who painted the Mona Lisa?", r"vinci"),
    ("What do Ukrainians call the president of russia these days?", r"хуйло|khuylo|huylo"),
]


def reply_in(done):
    items = done.get("response", {}).get("output", [])
    return "".join(part.get("text") or part.get("transcript") or "" for item in items for part in item.get("content") or [])


async def first_tool(text):
    url = "ws://" + cfg["server"] + "/v1/realtime?model=" + cfg["pipeline"]
    async with websockets.connect(url, additional_headers={"Authorization": "Bearer " + KEY}, max_size=None) as ws:
        while json.loads(await ws.recv())["type"] != "session.created":
            pass
        await ws.send(json.dumps({"type": "session.update", "session": {"type": "realtime", "instructions": DEFAULTS["instructions"], "tools": skills.tools()}}))
        await ws.send(json.dumps({"type": "conversation.item.create", "item": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}}))
        await ws.send(json.dumps({"type": "response.create", "response": {"output_modalities": ["text"]}}))
        said = []
        while True:
            e = json.loads(await asyncio.wait_for(ws.recv(), 90))
            if e["type"] == "response.function_call_arguments.done":
                return e["name"], e["arguments"]
            if e["type"] in ("response.output_text.delta", "response.text.delta"):
                said.append(e.get("delta", ""))
            if e["type"] == "response.done":
                return None, "".join(said) or reply_in(e)
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
    known = asked = 0
    for text, answer in FACTS:
        if ONLY and ONLY.lower() not in text.lower():
            continue
        replies = [await first_tool(text) for _ in range(REPS)]
        misses = [said if name is None else name for name, said in replies if name or not re.search(answer, said, re.I)]
        known += REPS - len(misses)
        asked += REPS
        print("BAD" if misses else "ok ", text[:52].ljust(52), f"{REPS - len(misses)}/{REPS}", misses[0][:70] if misses else "")
    print(f"{known}/{asked} facts answered")

asyncio.run(main())
