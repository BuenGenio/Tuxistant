"""Random edits, additions, removals, breakages, pipes, swaps and restarts of the skills you add,
one to three at a time, checked after every reload against what docs/SKILLS.md promises of the
six-action budget:

  I1  at most six actions of yours load, and no two tools anywhere share a name;
  I2  one whose file was what loaded at the step before, and is unchanged, still loads as it was;
  I3  one that loaded at the step before and was edited since, with no restart, has a version in
      use, whether the edit loads or not;
  I4  no other skill uses an id or action one of yours held at the step before and whose file
      is still there, unless the edit to it that gave that name up has just loaded;
  I5  no action of yours is a built-in one, and no id of yours one of Tuxistant's;
  I6  refused edits that could all load together, as in a swap, don't all stay refused;
  I7  right after a restart, tuxistant doctor refuses the same skills and loads the same tools;
  I8  a reload with nothing changed changes nothing, not even a reason.
"""
import os
import random
import unittest
from pathlib import Path

import yaml

from tests import test_recipes as tr
from tests.test_recipes import CONFIG, recipe
from tuxistant import skillhost

STEPS = 300
SEEDS = (7, 9, 11, 23)
POOL = ["a.one", "a.two", "b.one", "b.two", "c.one", "c.two", "d.one", "e.one", "f.one",
        "time.now", "file.x", "a_one.x", "a.one_x", "b_one.x"]
FORBIDDEN = {"time.now", "file.x"}      # a built-in action, and one that starts like one
IDS = ["org.example.a", "org.example.b", "org.example.c", "org.example.d", "org.example.e",
       "org.tuxistant.time"]
FILES = ["a.yaml", "b.yaml", "c.yaml", "d.yaml", "e.yaml", "f.yaml"]
BUILTIN_ACTIONS = {"time.now", "weather.forecast", "app.open"}


def tool(name: str) -> str:
    return name.replace(".", "_")


def text(skill_id: str, actions: list) -> str:
    return yaml.safe_dump(recipe(id=skill_id, permissions={}, actions={
        name: {"description": "Say", "say": "Hi."} for name in actions}), sort_keys=False)


def parsed(path: Path) -> dict | None:
    """id and actions of a recipe as written, when it is one."""
    try:
        data = yaml.safe_load(path.read_text()) if path.is_file() else None
    except (yaml.YAMLError, OSError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("actions"), dict):
        return None
    return {"id": data.get("id"), "actions": set(data["actions"])}


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

    def change(self, rng: random.Random) -> None:
        name = rng.choice(FILES)
        path = self.user / name
        op = rng.random()
        if path.exists() and not path.is_file():
            path.unlink()
        if op < 0.45:
            skill_id = rng.choice(IDS[:5] + [f"org.example.{name[0]}"] * 4)
            while True:
                actions = rng.sample(POOL, rng.choice([1, 1, 2, 2, 3]))
                if len({tool(a) for a in actions}) == len(actions):
                    break
            path.write_text(text(skill_id, actions))
        elif op < 0.53:
            path.write_text("id: [broken")
        elif op < 0.63:
            path.unlink(missing_ok=True)
        elif op < 0.66:
            path.unlink(missing_ok=True)
            os.mkfifo(path)
        else:   # a swap: this one takes the other's actions, and the other this one's
            other = self.user / rng.choice(FILES)
            mine, theirs = parsed(path), parsed(other)
            if other != path and mine and theirs:
                a, b = yaml.safe_load(path.read_text()), yaml.safe_load(other.read_text())
                a["actions"], b["actions"] = b["actions"], a["actions"]
                path.write_text(yaml.safe_dump(a, sort_keys=False))
                other.write_text(yaml.safe_dump(b, sort_keys=False))

    def run_steps(self, rng: random.Random) -> list:
        host = self.restart()
        host.reload()
        before_texts, before_loaded, before_used, held, held_ids = {}, {}, {}, {}, {}
        for step in range(STEPS):
            for _ in range(rng.choice([1, 1, 2, 3])):
                self.change(rng)
            restarted = rng.random() < 0.1
            if restarted:
                host = self.restart()
            problems = host.reload()
            present = {p.name for p in self.user.iterdir()}
            texts = {p.name: p.read_text() if p.is_file() else "<not a file>"
                     for p in self.user.iterdir()}
            mine = {Path(k).name: f for k, f in host.last_offered.items()
                    if Path(k).parent == self.user}
            loaded = {key: frozenset(f.manifest["actions"]) for key, f in mine.items()}
            ids = {key: f.manifest["id"] for key, f in mine.items()}
            edit_loaded = {key for key, f in mine.items() if f.text == texts.get(key)}
            actions = host.current().actions
            yours = {k for k, spec in actions.items() if spec.get("source") == "yours"}
            where = (f"step {step} restarted={restarted}",
                     {Path(k).name: why[:80] for k, why in problems.items()})
            tools = [tool(name) for name in actions]
            if len(yours) > 6 or len(tools) != len(set(tools)):
                return [("I1", where, sorted(actions))]
            if yours & BUILTIN_ACTIONS or any(
                    actions[k]["skill"].startswith("org.tuxistant.") for k in yours):
                return [("I5", where, sorted(yours))]
            for key, names in before_loaded.items():
                if key not in present:
                    continue
                unchanged = texts[key] == before_texts.get(key) == before_used.get(key)
                if unchanged and loaded.get(key) != names:
                    return [("I2", where, key, sorted(names))]
                if (texts[key] != before_texts.get(key) and not restarted
                        and key not in loaded and texts[key] != "<not a file>"):
                    return [("I3", where, key)]
            for key, names in held.items():
                if key not in present:
                    continue
                kept = names if key not in edit_loaded else names & loaded[key]
                kept_id = held_ids[key] if key not in edit_loaded or (
                    ids[key] == held_ids[key]) else None
                for other in loaded:
                    if other != key and ({tool(n) for n in kept} & {
                            tool(n) for n in loaded[other]} or kept_id == ids[other]):
                        return [("I4", where, key, other, sorted(kept), sorted(loaded[other]))]
            stuck = self.stuck(host, problems)
            if stuck:
                return [("I6", where, stuck)]
            owners = {k: spec["skill"] for k, spec in actions.items()}
            again = host.reload()
            if again != problems or owners != {
                    k: spec["skill"] for k, spec in host.current().actions.items()}:
                return [("I8", where, {Path(k).name: why[:80] for k, why in again.items()})]
            if restarted:
                doctor = skillhost.offline(CONFIG, builtin=self.builtin, bundled=self.bundled,
                                           user=self.user)
                skills = {k: spec["skill"] for k, spec in doctor.current().actions.items()}
                if set(doctor.problems) != set(problems) or skills != {
                        k: spec["skill"] for k, spec in actions.items()}:
                    return [("I7", where, {Path(k).name: why[:80]
                                           for k, why in doctor.problems.items()})]
            held = {k: v for k, v in held.items() if k in present} | loaded
            held_ids = {k: v for k, v in held_ids.items() if k in present} | ids
            before_texts, before_loaded = texts, loaded
            before_used = {key: f.text for key, f in mine.items()}
        return []

    def stuck(self, host, problems: dict) -> list:
        """Refused edits whose files, all loaded together, would clash with nothing else and
        fit the budget: a swap or a ring that a reload should have let in."""
        edits = {}
        for key in problems:
            path = Path(key)
            edit = parsed(path) if path.parent == self.user else None
            if edit and self.allowed(host, path, edit):
                edits[key] = edit
        if len(edits) < 2:
            return []
        others = {key: place for key, place in host.offered.items()
                  if Path(key).parent == self.user and key not in edits}
        taken = {tool(n) for place in others.values() for n in place.actions}
        taken |= {tool(n) for n in host.current().actions
                  if host.current().actions[n].get("source") != "yours"}
        taken_ids = {place.skill_id for place in others.values()}
        names, skill_ids = [], []
        for edit in edits.values():
            names += [tool(n) for n in edit["actions"]]
            skill_ids.append(edit["id"])
        count = sum(place.count or 0 for place in others.values()) + len(names)
        if (len(names) == len(set(names)) and len(skill_ids) == len(set(skill_ids))
                and not set(names) & taken and not set(skill_ids) & taken_ids
                and count <= skillhost.USER_TOOLS):
            return sorted(Path(k).name for k in edits)
        return []

    @staticmethod
    def allowed(host, path: Path, edit: dict) -> bool:
        """Allowed on its own: nothing forbidden, and new or no bigger than its place."""
        place = host.offered.get(str(path))
        return (edit["id"] not in IDS[-1:] and not edit["actions"] & FORBIDDEN and (
            place is None or place.count is not None and len(edit["actions"]) <= place.count))


if __name__ == "__main__":
    unittest.main()
