"""Random hand edits to skill-grants.json, of every type and size, odd names, links, pipes, and
changes while a reload runs, beside recipe edits that change which hosts a skill reaches,
renames, copies, breakages and restarts. After every reload:

  G1  the reload doesn't fail;
  G2  the built-in skills load;
  G3  no skill of yours reaches a host unless the file as the reload found it allows it, by a
      well-formed record, or has none for it, or one marked removed, for a first sight; with a
      file that can't be read, none reaches any host;
  G4  and the file as the reload left it notes each host reached;
  G5  no host is newly allowed to a skill whose file never left the folder since its record
      was noted, unless a hand edit allowed it or marked it removed.
"""
import json
import os
import random
import unittest
from pathlib import Path
from unittest import mock

import yaml

from tests import test_recipes as tr
from tests.test_recipes import EXAMPLES
from tuxistant import skillhost

STEPS = 250
SEEDS = (3, 5, 8)
HOSTS = {"coins": ("api.coins.example", "evil.example"),
         "notes": ("ntfy.example", "evil2.example")}
IDS = ["org.example.coins", "org.example.notes", "org.example.work", "org.example.gone",
       "org.tuxistant.lights"]
NAMES = ["coins.yaml", "coins2.yaml", "zcopy.yaml", "notes.yaml", "work.yaml"]
BUILT_IN = {"time.now", "weather.forecast", "app.open"}
ODD = ["a\u0000b", "a\ud800b", "../x", "", ".", "x" * 300]
VALUES = [None, True, False, 0, 1, -1, 2 ** 53, 10 ** 400, 1e308, -1e308, "x", "", [], {}, [1],
          ["api.coins.example"], ["evil.example", "api.coins.example"], ["ntfy.example"],
          {"network": True}, {"network": 5}, {"network": ["api.coins.example"]}] + ODD


def strict(path: Path):
    """What each id may reach by the file, as Tux reads it: {id: (removed, hosts or True)},
    or None for a file that is there but can't be trusted."""
    try:
        if not os.path.lexists(path):
            return {}
        if path.is_symlink() and (Path(os.path.realpath(path)).parent
                                  != Path(os.path.realpath(path.parent))):
            return None
        if not path.is_file() or path.stat().st_size > skillhost.MAX_STATE_BYTES:
            return None
        data = json.loads(path.read_text())
    except (OSError, ValueError, RecursionError):
        return None
    if not isinstance(data, dict) or not all(
            skillhost.SKILL_ID.fullmatch(k) and isinstance(v, dict) for k, v in data.items()):
        return None
    out = {}
    for skill_id, record in data.items():
        permissions = record.get("permissions", {})
        network = permissions.get("network") if isinstance(permissions, dict) else None
        config = record.get("config", [])
        shaped = (isinstance(permissions, dict)
                  and (network is None or isinstance(network, bool) or (
                      isinstance(network, list) and all(isinstance(h, str) for h in network)))
                  and isinstance(config, list) and all(isinstance(c, str) for c in config)
                  and isinstance(record.get("removed", False), bool)
                  and isinstance(record.get("folder", ""), str))
        if not shaped:
            out[skill_id] = (False, set())
        else:
            out[skill_id] = (record.get("removed", False),
                             True if network is True else set(network or ()))
    return out


def covers(granted, hosts: set) -> bool:
    return granted is True or hosts <= granted


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
            record = data.get(skill_id := rng.choice(IDS))
            if not isinstance(record, dict):
                record = data[skill_id] = {}
            field = rng.choice(["permissions", "config", "folder", "removed", "removed_at"])
            record[field] = rng.choice(VALUES + NAMES)
            self.clear()
            path.write_text(json.dumps(data))
        elif op < 0.52:     # a record replaced whole, or a key no skill could have
            data[rng.choice(IDS + ODD)] = rng.choice(VALUES)
            self.clear()
            path.write_text(json.dumps(data))
        elif op < 0.58:
            self.clear()
            path.write_text(rng.choice(["{damaged", "[" * 100000 + "]" * 100000, "null", "5",
                                        " " * (skillhost.MAX_STATE_BYTES + 1)]))
        elif op < 0.62:
            self.clear()
            os.mkfifo(path)
        elif op < 0.70:     # links: nowhere, beside, into another folder
            self.clear()
            (self.root / "away").mkdir(exist_ok=True)
            target = rng.choice([self.root / "nowhere.json", self.user.parent / "beside.json",
                                 self.root / "away" / "grants.json"])
            if rng.random() < 0.7 and target.name != "nowhere.json":
                target.write_text(json.dumps(data))
            path.symlink_to(target)
        elif op < 0.78:
            self.clear()

    def recipe_edit(self, rng: random.Random, sticky: dict) -> None:
        name = rng.choice(NAMES)
        path = self.user / name
        op = rng.random()
        if op < 0.12:
            path.unlink(missing_ok=True)
            sticky.pop(name, None)
        elif op < 0.22:
            path.write_text("id: [broken")
        elif op < 0.32:     # a rename
            other = self.user / rng.choice(NAMES)
            if path.exists() and other != path and not other.exists():
                os.rename(path, other)
                if name in sticky:
                    sticky[other.name] = sticky.pop(name)
        else:
            base = rng.choice(["coins", "notes", "work"])
            text = (EXAMPLES / f"{base}.yaml").read_text()
            if base in HOSTS and rng.random() < 0.4:
                text = text.replace(*HOSTS[base])
            path.write_text(text)

    @staticmethod
    def reads(path: Path) -> bool:
        try:
            return isinstance(yaml.safe_load(path.read_text()), dict)
        except (yaml.YAMLError, OSError, UnicodeError):
            return False

    def run_steps(self, rng: random.Random) -> list:
        host = self.restart()
        sticky: dict[str, str] = {}     # file name: the id it last read as
        granted: dict[str, object] = {}     # id: the hosts its record in force allows
        free: set[str] = set()          # ids that may be allowed more: gone, or by hand
        last_post = strict(self.grants())
        for step in range(STEPS):
            for _ in range(rng.choice([1, 1, 2])):
                if rng.random() < 0.5:
                    self.hand_edit(rng)
                else:
                    self.recipe_edit(rng, sticky)
            for path in self.user.iterdir():
                try:
                    data = yaml.safe_load(path.read_text()) if path.is_file() else None
                except (yaml.YAMLError, OSError, UnicodeError):
                    data = None
                if isinstance(data, dict) and isinstance(data.get("id"), str):
                    sticky[path.name] = data["id"]
            pre = strict(self.grants())
            if pre != last_post:        # a hand edit: what it says is in force
                if pre is not None:
                    for skill_id in set(IDS) | set(pre) | set(last_post or {}):
                        record = pre.get(skill_id)
                        if record is None or record[0]:
                            free.add(skill_id)
                        else:
                            granted[skill_id] = record[1]
            free |= {skill_id for skill_id in IDS if skill_id not in sticky.values()}
            if rng.random() < 0.1:
                host = self.restart()   # which can't know the id of a file that won't read
                loaded = self.user.parent / skillhost.LOADED   # and had no place
                places = json.loads(loaded.read_text()) if loaded.is_file() else {}
                for name in list(sticky):
                    path = self.user / name
                    if name not in places and not self.reads(path):
                        del sticky[name]
                free |= {skill_id for skill_id in IDS if skill_id not in sticky.values()}
            touch = rng.random() < 0.1
            save, calls = skillhost._Grants.save, []

            def touched(grants):
                calls.append(1)
                if len(calls) <= 1 and grants.file.is_file():
                    os.utime(grants.file, ns=(1, 10 ** 18 + step))
                return save(grants)
            with mock.patch.object(skillhost._Grants, "save", touched if touch else save):
                try:
                    problems = host.reload()
                except Exception as e:      # reload never raises
                    return [("G1", f"step {step}", repr(e))]
            post = strict(self.grants())
            actions = host.current().actions
            where = f"step {step} touch={touch}", {Path(k).name: v[:70]
                                                   for k, v in problems.items()}
            if "skills" in problems:
                return [("G1", where)]
            if not BUILT_IN <= set(actions):
                return [("G2", where, sorted(actions))]
            reach = {}
            for spec in actions.values():
                if spec.get("source") == "yours":
                    reach.setdefault(spec["skill"], set()).update(
                        spec.get("permissions", {}).get("network") or ())
            for skill_id, hosts in reach.items():
                if not hosts:
                    continue
                record = None if pre is None else pre.get(skill_id)
                if pre is None or (record and not record[0] and not covers(record[1], hosts)):
                    return [("G3", where, skill_id, sorted(hosts), record)]
                kept = None if post is None else post.get(skill_id)
                if not kept or kept[0] or not covers(kept[1], hosts):
                    return [("G4", where, skill_id, sorted(hosts), kept)]
                if (skill_id in granted and skill_id not in free
                        and not covers(granted[skill_id], hosts)):
                    return [("G5", where, skill_id, sorted(hosts), granted[skill_id])]
            for skill_id, record in (post or {}).items():
                if not record[0]:
                    granted[skill_id] = record[1]
                    free.discard(skill_id)
            last_post = post
        return []


if __name__ == "__main__":
    unittest.main()
