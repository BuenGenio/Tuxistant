"""Random hand edits to skill-grants.json, of every type and size, links, pipes and damage while
Tux runs, beside edits that change which hosts a recipe reaches, and restarts. After every
reload:

  G1  the reload doesn't fail;
  G2  the built-in skills load;
  G3  no skill of yours reaches a host that the file on disk, read strictly, doesn't allow it
      by a well-formed record, which a first sight writes there; with a file that can't be
      read, none reaches any host at all.
"""
import json
import os
import random
import unittest
from pathlib import Path

from tests import test_recipes as tr
from tests.test_recipes import EXAMPLES
from tuxistant import skillhost

STEPS = 250
SEEDS = (3, 5, 8)
RECIPES = {"coins.yaml": ("api.coins.example", "evil.example"),
           "notes.yaml": ("ntfy.example", "evil2.example"),
           "work.yaml": None}
IDS = ["org.example.coins", "org.example.notes", "org.example.work", "org.example.gone",
       "org.tuxistant.lights"]
BUILT_IN = {"time.now", "weather.forecast", "app.open"}
VALUES = [None, True, False, 0, 1, -1, 2 ** 53, 10 ** 400, 1e308, -1e308, "x", "", [], {}, [1],
          ["api.coins.example"], ["evil.example", "api.coins.example"], ["ntfy.example"],
          {"network": True}, {"network": 5}, {"network": ["api.coins.example"]}]


def allowed(path: Path) -> dict | None:
    """The hosts each id may reach by the file on disk, read strictly, or None when the file
    is there but isn't a mapping of ids to well-formed records."""
    try:
        if not os.path.lexists(path):
            return {}
        if not path.is_file() or path.stat().st_size > skillhost.MAX_STATE_BYTES:
            return None
        data = json.loads(path.read_text())
    except (OSError, ValueError, RecursionError):
        return None
    if not isinstance(data, dict) or not all(isinstance(v, dict) for v in data.values()):
        return None
    hosts = {}
    for skill_id, record in data.items():
        permissions = record.get("permissions")
        network = permissions.get("network") if isinstance(permissions, dict) else None
        if record.get("removed") is not False and "removed" in record:
            continue
        if network is True:
            hosts[skill_id] = True
        elif isinstance(network, list) and all(isinstance(h, str) for h in network):
            hosts[skill_id] = set(network)
    return hosts


class GrantsFuzz(tr.Case):
    restart = tr.Host.restart

    def test_no_hand_edit_wins_a_host(self):
        for seed in SEEDS:
            with self.subTest(seed=seed):
                for path in self.user.iterdir():
                    path.unlink()
                for name in (skillhost.LOADED, skillhost.GRANTS):
                    path = self.user.parent / name
                    if os.path.lexists(path):
                        path.unlink()
                self.assertEqual(self.run_steps(random.Random(seed)), [])

    def grants(self) -> Path:
        return self.user.parent / skillhost.GRANTS

    def clear(self) -> None:
        path = self.grants()
        if path.is_dir() and not path.is_symlink():
            path.rmdir()
        elif os.path.lexists(path):
            path.unlink()

    def hand_edit(self, rng: random.Random) -> None:
        path = self.grants()
        op = rng.random()
        try:
            data = json.loads(path.read_text()) if path.is_file() else {}
        except (OSError, ValueError, RecursionError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        if op < 0.45:       # a field of a record set to anything
            record = data.setdefault(rng.choice(IDS), {})
            if not isinstance(record, dict):
                record = data[rng.choice(IDS)] = {}
            field = rng.choice(["permissions", "config", "folder", "removed", "removed_at"])
            record[field] = rng.choice(VALUES + ["coins.yaml", "notes.yaml"])
            self.clear()
            path.write_text(json.dumps(data))
        elif op < 0.55:     # a record replaced whole
            data[rng.choice(IDS)] = rng.choice(VALUES)
            self.clear()
            path.write_text(json.dumps(data))
        elif op < 0.62:
            self.clear()
            path.write_text(rng.choice(["{damaged", "[" * 100000 + "]" * 100000, "null", "5",
                                        '{"x": 1}', " " * (skillhost.MAX_STATE_BYTES + 1)]))
        elif op < 0.67:
            self.clear()
            os.mkfifo(path)
        elif op < 0.72:
            self.clear()
            path.symlink_to(self.root / rng.choice(["nowhere.json", "other.json"]))
            (self.root / "other.json").write_text(json.dumps({"theme": "dark"}))
        elif op < 0.80:
            self.clear()

    def recipe_edit(self, rng: random.Random) -> None:
        name = rng.choice(list(RECIPES))
        path = self.user / name
        hosts = RECIPES[name]
        if rng.random() < 0.15:
            path.unlink(missing_ok=True)
            return
        text = (EXAMPLES / name).read_text()
        if hosts and rng.random() < 0.4:
            text = text.replace(hosts[0], hosts[1])
        path.write_text(text)

    def run_steps(self, rng: random.Random) -> list:
        host = self.restart()
        for step in range(STEPS):
            for _ in range(rng.choice([1, 1, 2])):
                (self.hand_edit if rng.random() < 0.6 else self.recipe_edit)(rng)
            if rng.random() < 0.1:
                host = self.restart()
            problems = host.reload()
            actions = host.current().actions
            where = f"step {step}", {Path(k).name: why[:80] for k, why in problems.items()}
            if "skills" in problems:
                return [("G1", where)]
            if not BUILT_IN <= set(actions):
                return [("G2", where, sorted(actions))]
            may = allowed(self.grants())
            for name, spec in actions.items():
                reach = set(spec.get("permissions", {}).get("network") or ())
                if spec.get("source") != "yours" or not reach:
                    continue
                granted = None if may is None else may.get(spec["skill"])
                if granted is not True and not reach <= (granted or set()):
                    return [("G3", where, name, sorted(reach), granted)]
        return []


if __name__ == "__main__":
    unittest.main()
