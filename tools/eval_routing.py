#!/usr/bin/python3
"""Which tool does the real model pick? Run this after adding a tool or changing a description.

    python3 tools/eval_routing.py [--server host:port] [repeats] [text to filter on]
    TUX_EVAL_HOME=1 python3 tools/eval_routing.py       with Home Assistant's tools and cases

Nothing is executed: a turn stops at the first tool call. Needs the LocalAI server. The small
model is consistent, so a request that goes wrong usually goes wrong every time, and one tool's
wording can move requests that seem unrelated. Add a case whenever a real request goes wrong.

Home Assistant's two tools are offered only once it is set up, so they and their cases are left
out unless TUX_EVAL_HOME=1. The address and token are then made up, here only: the ones in
Settings are never read, and nothing is ever sent to a home.
"""
import asyncio
import collections
import json
import os
import re
import sys
import time
from pathlib import Path

import websockets

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tools.eval_config import evaluation_server
from tuxistant.config import Config, DEFAULTS
from tuxistant.skillhost import offline

cfg = Config()
evaluation_server(cfg)
KEY = cfg["api_key"]
HOME = os.environ.get("TUX_EVAL_HOME") == "1"
# Lights are offered only with a TP-Link account. Made up too, and no code skill is started.
SET_UP = {"tapo_email": "eval@example.test", "tapo_password": "eval",
          **({"ha_server": "eval.invalid:8123", "ha_token": "eval"} if HOME else {})}
# The tools the daemon offers, code skills too, and mail search, which is off until turned on.
skills = offline({**DEFAULTS, "mail_search": True, **SET_UP}).current()
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
    ("When did I apply to renew my passport?", {"mail_search"}),
    ("Did the bank email me about my new card?", {"mail_search"}),
    ("What did my landlord say about the rent?", {"mail_search"}),
    ("Find the email from the dentist", {"mail_search"}),
    ("When did I order the new laptop?", {"mail_search"}),
    ("Has the airline written to me about my flight?", {"mail_search"}),
    ("What's my booking reference for the hotel in Lisbon?", {"mail_search"}),
    ("Search the web for electric bike reviews", {"web_search"}),
    ("Look up the opening hours of the Louvre", {"web_search"}),
    ("Search for the latest Ubuntu release", {"web_search"}),
    ("What's the price of bitcoin?", {"web_search"}),
    ("Research electric bikes for me", {"research_run"}),
    ("Do some research on heat pumps", {"research_run"}),
    ("Compare the Framework laptop and the ThinkPad X1", {"research_run", "web_search"}),
    ("What's the capital of France?", None),
    ("Tell me a joke", None),
    # Either will do: a calendar entry, with a card, or a reminder that Tux says out loud.
    ("Remind me to call the dentist tomorrow", {"calendar_add", "timer_set"}),
    ("Send an email to Anna", None),
    ("Pause the music", {"media_control"}),
    ("Pause", {"media_control"}),
    ("Resume the music", {"media_control"}),
    ("Play the music again", {"media_control"}),
    ("Next song", {"media_control"}),
    ("Skip this song", {"media_control"}),
    ("Previous song", {"media_control"}),
    ("Go back a song", {"media_control"}),
    ("Stop the music", {"media_control"}),
    ("Pause Spotify", {"media_control"}),
    ("Pause the video", {"media_control"}),
    ("Turn it up", {"media_control"}),
    ("Turn the volume down", {"media_control"}),
    ("Louder", {"media_control"}),
    ("It's too loud", {"media_control"}),
    ("Volume 70", {"media_control"}),
    ("Set the volume to 40 percent", {"media_control"}),
    ("Mute", {"media_control"}),
    ("Unmute", {"media_control"}),
    ("What's playing?", {"media_playing"}),
    ("What song is this?", {"media_playing"}),
    ("Who sings this?", {"media_playing"}),
    ("What am I listening to?", {"media_playing"}),
    # Near misses: none of these is for the media tools.
    ("Turn the lamp down", {"lights_set"}),
    ("Set the light to 70 percent", {"lights_set"}),
    ("Brightness 50", {"lights_set"}),
    ("Turn the light up", {"lights_set"}),
    ("Stop", None),
    ("Stop talking", None),
    ("Be quiet", None),
    ("Shut up", None),
    ("How do you say music in Spanish?", {"translate_text"}),
    ("How do you say turn it up in German?", {"translate_text"}),
    ("Is Spotify running?", {"system_running"}),
    ("Set a timer for 10 minutes", {"timer_set"}),
    ("Set a pasta timer for 8 minutes", {"timer_set"}),
    ("Remind me in half an hour to stretch", {"timer_set"}),
    ("Tell me when 20 minutes are up", {"timer_set"}),
    ("Remind me at 5pm to call mum", {"timer_set"}),
    ("Remind me tomorrow morning to take the bins out", {"timer_set"}),
    ("Wake me at 7", {"timer_set"}),
    ("What timers are running?", {"timer_list"}),
    ("How long is left on the pasta?", {"timer_list"}),
    ("What reminders do I have?", {"timer_list"}),
    ("Cancel the pasta timer", {"timer_cancel"}),
    ("Cancel my timers", {"timer_cancel"}),
    ("Stop the timer", {"timer_cancel"}),
    # Near misses: the calendar keeps what says calendar, schedule or book.
    ("Put a reminder to pay the rent in my calendar for Friday", {"calendar_add"}),
    ("Schedule a call with mum on Friday at 5", {"calendar_add"}),
    ("What do I have on tomorrow?", {"calendar_events"}),
    # Nothing can do these, and a timer must not pretend to.
    ("Cancel my 3pm meeting", None),
    ("Turn off the lights in ten minutes", None),
    ("Open Spotify", None),
    ("Play some jazz", None),
    ("Next", {"media_control"}),
    ("Half volume", {"media_control"}),
    ("Cancel", None),
    ("Never mind", None),
]

# Only with TUX_EVAL_HOME=1, on top of everything above. With both set up, "the light" with
# nothing named stays with lights_set above, and a named device or room belongs here.
HOME_CASES = [
    ("Turn on the kitchen light", {"home_set"}),
    ("Switch off the living room lights", {"home_set"}),
    ("Dim the bedroom lamp to 30 percent", {"home_set", "lights_set"}),
    ("Turn off the TV", {"home_set"}),
    ("Turn on the fan in the bedroom", {"home_set"}),
    ("Lock the front door", {"home_set"}),
    ("Unlock the front door", {"home_set"}),
    ("Open the garage door", {"home_set"}),
    ("Close the bedroom blinds", {"home_set"}),
    ("Set the heating to 21", {"home_set"}),
    ("Make it warmer in the living room, 22 degrees", {"home_set"}),
    ("Activate movie time", {"home_set"}),
    ("Is the front door locked?", {"home_status"}),
    ("Is the garage door open?", {"home_status"}),
    ("Which lights are on?", {"home_status", "lights_status"}),
    ("What's the temperature in the bedroom?", {"home_status"}),
    ("Are any windows open?", {"home_status"}),
    ("Disarm the alarm", {"home_set"}),     # it reaches the handler, which refuses in words
    # Near misses: a room in a request for music or the weather is not a device in that room.
    ("Play some jazz in the kitchen", None),
    ("What's the temperature?", {"weather_forecast"}),
    ("Turn off the computer", {"shell_run"}),
    ("Lock the screen", {"shell_run", None}),
    ("What's the temperature outside?", {"weather_forecast"}),
    ("How do you say the light is on in French?", {"translate_text"}),
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
    ("Who wrote Bohemian Rhapsody?", r"mercury|queen"),
    # About time, and not for a timer. They are here and not above because they are facts:
    # eval_facts takes every case above as something that must not be sorted as one.
    ("How many minutes are there in a day?", r"1,?440|fourteen hundred"),
    ("How long does it take to boil an egg?", r"minute"),
]
# Only with TUX_EVAL_HOME=1: about light and heat, and not for a home.
HOME_FACTS = [
    ("What temperature does water boil at?", r"100|212|hundred"),
    ("Who invented the light bulb?", r"edison|swan"),
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
    for text, wanted in CASES + (HOME_CASES if HOME else []):
        if ONLY and ONLY.lower() not in text.lower():
            continue
        seen = collections.Counter()
        if HOME and text == "Switch off the bedroom lamp":
            wanted = {"lights_set", "home_set"}
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
    for text, answer in FACTS + (HOME_FACTS if HOME else []):
        if ONLY and ONLY.lower() not in text.lower():
            continue
        replies = [await first_tool(text) for _ in range(REPS)]
        misses = [said if name is None else name for name, said in replies
                  if name != "knowledge_answer" and (name or not re.search(answer, said, re.I))]
        known += REPS - len(misses)
        asked += REPS
        print("BAD" if misses else "ok ", text[:52].ljust(52), f"{REPS - len(misses)}/{REPS}", misses[0][:70] if misses else "")
    print(f"{known}/{asked} facts answered")

asyncio.run(main())
