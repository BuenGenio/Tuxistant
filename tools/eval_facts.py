#!/usr/bin/python3
"""Does Tux answer facts from what's written down, and does it make things up?

    python3 tools/eval_facts.py [--server host:port] [--reps 2] [--pause 3] [--sort-only]

Runs the real route on a hold turn, without the audio: the sorter, then Wikipedia, web search and
the real model. What matters most is the WRONG column, as a wrong answer sounds exactly like a
right one. Each question comes in rewordings, because a 4B model that gets one wording right can
miss the next.

Web search is run gently: search engines block a server that asks too fast, and a block lasts
hours. That's --pause, in seconds between questions. Nothing here touches the desktop.
"""
import argparse
import ast
import asyncio
import collections
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tuxistant import knowledge  # noqa: E402
from tools.eval_config import evaluation_server
from tuxistant.config import Config  # noqa: E402
from tuxistant.gate import Session  # noqa: E402

EASY = [
    (("How many chemical elements are there?", "How many elements are in the periodic table?"), r"118"),
    (("Who wrote Pride and Prejudice?", "Who's the author of Pride and Prejudice?"), r"austen"),
    (("How many bones are in the human body?", "How many bones does an adult have?"), r"206"),
    (("What's the speed of light?", "How fast does light travel?"), r"299|300,?000|186"),
    (("When did World War Two end?", "What year did the Second World War finish?"), r"1945"),
    (("What year did the Titanic sink?", "When did the Titanic go down?"), r"1912"),
    (("What is the tallest mountain in the world?", "What's the highest mountain on Earth?"), r"everest"),
    (("Who painted the Mona Lisa?", "Who was the painter of the Mona Lisa?"), r"vinci"),
    (("What is the capital of France?", "What's France's capital city?"), r"paris"),
    (("How many continents are there?", "How many continents does the world have?"), r"seven|\b7\b"),
    (("What is the chemical symbol for gold?", "What's gold's symbol on the periodic table?"), r"\bau\b"),
    (("How many strings does a violin have?", "How many strings are on a violin?"), r"four|\b4\b"),
]
HARD = [
    (("What is the rarest element on Earth?", "What is the rarest element on the planet?",
      "Which natural element is the rarest?"), r"astatine"),
    (("What's the atomic number of molybdenum?", "Molybdenum has what atomic number?"), r"\b42\b|forty.two"),
    (("Which element has the symbol W?", "What element does W stand for in the periodic table?"), r"tungsten"),
    (("What is the capital of Burkina Faso?", "What's Burkina Faso's capital?"), r"ouagadougou"),
    (("What is the capital of Kyrgyzstan?", "What's Kyrgyzstan's capital city?"), r"bishkek"),
    (("Who won the Nobel Prize in Literature in 1953?", "Who got the 1953 Nobel Prize for Literature?"), r"churchill"),
    (("Who was the second person to walk on the moon?", "Who walked on the moon after Neil Armstrong?"), r"aldrin"),
    (("What is the smallest country in Africa?", "Which African country is the smallest?"), r"seychelles"),
    (("What is the deepest lake in the world?", "Which lake is the deepest on Earth?"), r"baikal"),
    (("What year was the Peace of Westphalia signed?", "When was the Peace of Westphalia?"), r"1648"),
    (("Who composed The Rite of Spring?", "Who wrote the music for The Rite of Spring?"), r"stravinsky"),
    (("What is the boiling point of liquid nitrogen in Celsius?",
      "At what temperature in Celsius does nitrogen boil?"), r"19[56]"),
    (("Who first isolated oxygen?", "Who discovered oxygen?"), r"priestley|scheele|lavoisier"),
]
# Things nothing has been written about. Check before adding one: Aubrium turned out to be an
# Instagram tool and Zubrowka a country in a film, and Tux rightly reported what pages said.
MADE_UP = [
    "What is the atomic number of aubrium?",
    "Who won the 1987 Nobel Prize in Cooking?",
    "What is the capital of the country of Velmoravia?",
    "Who wrote the novel The Glass Tortoise of Vienna?",
    "Which element has the symbol Qz?",
    "Who was the first person to walk on Neptune?",
]
# Never tuned against. The sorter's examples were chosen by looking at the lists above, so this is
# the honest score. If one of these goes wrong, don't fix it with an example from this list: move
# the case up, and write new ones here.
UNSEEN_FACTS = [
    "Who discovered penicillin?", "What's the largest ocean?",
    "How many planets are in the solar system?", "What's the freezing point of water in Fahrenheit?",
    "Who was the first president of the United States?", "What language do they speak in Brazil?",
    "What is the currency of Japan?", "What does DNA stand for?", "Who wrote War and Peace?",
    "What's the longest river in Africa?", "What is photosynthesis?", "Where is Machu Picchu?",
    "Who invented the telephone?", "What is the hardest natural substance?",
    "How far is the moon from the Earth?", "Who was Cleopatra?",
    "What's the chemical formula for salt?", "What's a group of crows called?",
    "Who was King Bartholomew the Ninth of Sweden?", "What's the population of Glimmerhaven?",
    "What does the word snorflax mean?", "Who painted The Purple Giraffe at Dawn?",
    "What is the boiling point of zentrite?", "Which river runs through the city of Oslandia?",
]
UNSEEN_OTHER = [
    "What's the temperature outside?", "What's Apple's share price?", "What time is it in Tokyo?",
    "How's the traffic?", "Who's at the door?", "What's the score in the Lakers game?",
    "What's trending today?", "Open Firefox", "Close this window", "Copy that", "Mute the sound",
    "What's the Ukrainian for goodbye?", "Translate this into German", "What does this error mean?",
    "What does this file say?", "What's my password?", "What's my address?",
    "How much battery do I have left?", "Am I connected to the internet?", "What am I working on?",
    "What's on my screen?", "Remind me to call mum", "Email my boss", "Good morning", "I'm tired",
    "Never mind", "How do I install Steam?", "Why is my computer slow?", "What's the latest iPhone?",
    "What's the German word for tree?",
]
SUMS = [
    ("What's 15 percent of 80?", "12"), ("What's seven times eight?", "56"),
    ("What's the square root of 144?", "12"), ("What is 1234 plus 5678?", "6,912"),
    ("What's 17 times 23?", "391"), ("How much is 1000 divided by 8?", "125"),
]
# What must go on being handled by the voice model and its tools, however it's worded.
NOT_FACTS = [
    "What time is it?", "What's the date today?", "How much space is left on my disk?",
    "What's the weather like tomorrow?", "Do I need a coat?", "Is it going to rain in Lisbon?",
    "What's the news?", "What's the price of bitcoin?", "Who won the football last night?",
    "Where's my phone?", "Ring my phone", "Find my CV", "Open my tax return",
    "How many files are in my Documents folder?", "What's in my Downloads folder?",
    "Which folder uses the most space?", "Delete my old log files", "Install VLC",
    "Is docker running?", "What's my IP address?", "How long has this computer been on?",
    "Translate where is the train station into Ukrainian", "How do you say cheese in Spanish?",
    "When is my next meeting?", "What's my wife's name?", "Who am I?", "What did I just say?",
    "Tell me about yourself", "What can you do?", "How are you?", "Thanks", "Tell me a joke",
    "Set a reminder for nine", "Search the web for cheap flights", "Research electric bikes",
    "Send this to my phone", "Read my emails", "Turn the volume up", "What's on my calendar?",
]


async def asked(question: str, config) -> str:
    session = Session()
    await knowledge.answer({"question": question}, config, session)
    return " ".join(item.text for item in session.take_after())


def routed_requests() -> list[str]:
    """Everything in the routing eval, which are real requests for tools."""
    tree = ast.parse((ROOT / "tools/eval_routing.py").read_text())
    named = ("CASES", "HOME_CASES")
    lists = [ast.literal_eval(node.value) for node in tree.body if isinstance(node, ast.Assign)
             and any(getattr(target, "id", "") in named for target in node.targets)]
    cases = [case for listed in lists for case in listed]
    return [request for request, _ in cases if request != "What's the capital of France?"]


async def sort_set(label: str, facts: list, others: list, config, reps: int) -> bool:
    """A fact missed costs an answer from memory. Anything else called a fact goes to Wikipedia,
    which is both a wrong answer and a private request leaving the house, so that one must be 0."""
    missed, leaked, seconds = collections.Counter(), collections.Counter(), []
    for question in [*facts, *others]:
        for _ in range(reps):
            started = time.monotonic()
            fact = await knowledge.is_fact(question, config)
            seconds.append(time.monotonic() - started)
            if question in others and fact:
                leaked[question] += 1
            elif question not in others and not fact:
                missed[question] += 1
    seconds.sort()
    print(f"Sorter, {label}: {len(facts) * reps} tries on facts, {sum(missed.values())} missed. "
          f"{len(others) * reps} tries on everything else, {sum(leaked.values())} CALLED A FACT. "
          f"Median {seconds[len(seconds) // 2]:.2f} s, slowest {seconds[-1]:.2f} s.", flush=True)
    for question, count in (*leaked.most_common(), *missed.most_common()):
        print(f"   {'LEAKED' if question in leaked else 'missed'} {count}x {question}", flush=True)
    return not leaked


async def sorter(config, reps: int) -> bool:
    facts = [q for cases in (EASY, HARD) for questions, _ in cases for q in questions] + MADE_UP
    results = [await sort_set("tuned on", facts, NOT_FACTS, config, reps),
               await sort_set("never seen", UNSEEN_FACTS, UNSEEN_OTHER, config, reps),
               await sort_set("routing requests", [], routed_requests(), config, reps)]
    return all(results)


async def facts(config, reps: int, pause: float) -> bool:
    tally, wrong, seconds = collections.Counter(), [], []
    cases = [("easy", questions, pattern) for questions, pattern in EASY]
    cases += [("hard", questions, pattern) for questions, pattern in HARD]
    cases += [("made up", (question,), None) for question in MADE_UP]
    for group, questions, pattern in cases:
        for question in questions:
            for _ in range(reps):
                started = time.monotonic()
                said = await asked(question, config)
                seconds.append(time.monotonic() - started)
                if said == knowledge.NO_LOOKUP:
                    tally[group, "failed"] += 1
                elif said == knowledge.DONT_KNOW:
                    tally[group, "unknown"] += 1
                elif pattern and re.search(pattern, said, re.I):
                    tally[group, "right"] += 1
                else:
                    tally[group, "wrong"] += 1
                    wrong.append(f"{group}: {question} -> {said}")
                await asyncio.sleep(pause)
    seconds.sort()
    for group in ("easy", "hard", "made up"):
        total = sum(n for (g, _), n in tally.items() if g == group)
        print(f"{group}: {total} tries, {tally[group, 'right']} right, "
              f"{tally[group, 'unknown']} said they didn't know, {tally[group, 'wrong']} WRONG, "
              f"{tally[group, 'failed']} couldn't look it up")
    print(f"Median {seconds[len(seconds) // 2]:.1f} s, slowest {seconds[-1]:.1f} s.")
    for line in wrong:
        print("  ", line)
    return not wrong


async def sums(config) -> bool:
    bad = 0
    for question, answer in SUMS:
        said = await asked(question, config)
        if said != f"That's {answer}.":
            bad += 1
            print("   WRONG", question, "->", said)
    print(f"Sums: {len(SUMS) - bad} of {len(SUMS)}.")
    return not bad


async def main():
    config = Config()
    evaluation_server(config)   # first, as it takes --server out of the arguments
    options = argparse.ArgumentParser()
    options.add_argument("--reps", type=int, default=2)
    options.add_argument("--pause", type=float, default=3)
    options.add_argument("--sort-only", action="store_true")
    args = options.parse_args()
    config.values["fact_lookup"] = True
    good = await sorter(config, args.reps)
    if not args.sort_only:
        good = await sums(config) and good
        good = await facts(config, args.reps, args.pause) and good
    sys.exit(0 if good else 1)


asyncio.run(main())
