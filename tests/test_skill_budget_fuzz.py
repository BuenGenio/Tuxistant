"""Random edits, additions, removals, breakages and restarts of the skills you add, checked
after every reload against what docs/SKILLS.md promises of the six-action budget:

  I1  at most six actions of yours load;
  I2  one whose file was what loaded at the step before, and is unchanged, still loads as it was;
  I3  one that loaded at the step before and was edited since, with no restart, has a version in
      use, whether the edit loads or not;
  I4  no other skill uses an id or action one of yours held at the step before and whose file
      is still there, unless the edit to it that gave that name up has just loaded;
  I5  no action of yours is a built-in one, and no id of yours one of Tuxistant's.
"""
import random
import unittest
from pathlib import Path

import yaml

from tests import test_recipes as tr
from tests.test_recipes import recipe
from tuxistant import skillhost

STEPS = 300
SEEDS = (7, 11, 23)
POOL = ["a.one", "a.two", "b.one", "b.two", "c.one", "c.two", "d.one", "e.one", "time.now",
        "file.x", "a_one.x"]
IDS = ["org.example.a", "org.example.b", "org.example.c", "org.example.d", "org.tuxistant.time"]
FILES = ["a.yaml", "b.yaml", "c.yaml", "d.yaml", "e.yaml"]
BUILTIN_ACTIONS = {"time.now", "weather.forecast", "app.open"}


def text(skill_id: str, actions: list) -> str:
    return yaml.safe_dump(recipe(id=skill_id, permissions={}, actions={
        name: {"description": "Say", "say": "Hi."} for name in actions}), sort_keys=False)


class Fuzz(tr.Case):
    restart = tr.Host.restart

    def test_the_budget_keeps_its_promises_whatever_is_done(self):
        for seed in SEEDS:
            with self.subTest(seed=seed):
                for path in self.user.iterdir():
                    path.unlink()
                for name in (skillhost.LOADED, skillhost.GRANTS):
                    (self.user.parent / name).unlink(missing_ok=True)
                self.assertEqual(self.run_steps(random.Random(seed)), [])

    def run_steps(self, rng: random.Random) -> list:
        host = self.restart()
        host.reload()
        before_texts, before_loaded, before_used, held, held_ids = {}, {}, {}, {}, {}
        for step in range(STEPS):
            op, name, restarted = rng.random(), rng.choice(FILES), False
            path = self.user / name
            if op < 0.45:
                skill_id = rng.choice(IDS[:4] + [f"org.example.{name[0]}"] * 3)
                path.write_text(text(skill_id, rng.sample(POOL, rng.choice([1, 1, 2, 2, 3]))))
            elif op < 0.55:
                path.write_text("id: [broken")
            elif op < 0.65:
                path.unlink(missing_ok=True)
            elif op < 0.75:
                host, restarted = self.restart(), True
            problems = host.reload()
            texts = {p.name: p.read_text() for p in self.user.iterdir() if p.is_file()}
            mine = {Path(k).name: f for k, f in host.last_offered.items()
                    if Path(k).parent == self.user}
            loaded = {key: frozenset(f.manifest["actions"]) for key, f in mine.items()}
            ids = {key: f.manifest["id"] for key, f in mine.items()}
            edit_loaded = {key for key, f in mine.items() if f.text == texts.get(key)}
            actions = host.current().actions
            yours = {k for k, spec in actions.items() if spec.get("source") == "yours"}
            where = (f"step {step} op {op:.2f} {name} restarted={restarted}",
                     {Path(k).name: why[:80] for k, why in problems.items()})
            if len(yours) > 6:
                return [("I1", where, sorted(yours))]
            if yours & BUILTIN_ACTIONS or any(
                    actions[k]["skill"].startswith("org.tuxistant.") for k in yours):
                return [("I5", where, sorted(yours))]
            for key, names in before_loaded.items():
                if key not in texts:
                    continue
                unchanged = texts[key] == before_texts.get(key) == before_used.get(key)
                if unchanged and loaded.get(key) != names:
                    return [("I2", where, key, sorted(names))]
                if texts[key] != before_texts.get(key) and not restarted and key not in loaded:
                    return [("I3", where, key)]
            for key, names in held.items():
                if key not in texts:
                    continue
                kept = names if key not in edit_loaded else names & loaded[key]
                kept_id = held_ids[key] if key not in edit_loaded or (
                    ids[key] == held_ids[key]) else None
                for other in loaded:
                    if other != key and (kept & loaded[other] or kept_id == ids[other]):
                        return [("I4", where, key, other, sorted(kept & loaded[other]))]
            held = {k: v for k, v in held.items() if k in texts} | loaded
            held_ids = {k: v for k, v in held_ids.items() if k in texts} | ids
            before_texts, before_loaded = texts, loaded
            before_used = {key: f.text for key, f in mine.items()}
        return []


if __name__ == "__main__":
    unittest.main()
