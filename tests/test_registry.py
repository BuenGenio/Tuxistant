"""Signing in Tux: the registry's index, its revocations and each recipe, checked offline with
ssh-keygen. Every key is made here in a temporary folder and thrown away, every file is served
by a fake fetch, and the state and skills folders are temporary. Skipped without ssh-keygen."""
import errno
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tuxistant import registry  # noqa: E402
from tuxistant.config import DEFAULTS  # noqa: E402
from tuxistant.registry import (INDEX_NS, RECIPE_NS, REVOCATIONS_NS, Refused,  # noqa: E402
                                Registry)
from tuxistant.skillhost import SkillHost  # noqa: E402
from tuxistant.skills import BUILTIN  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / "docs" / "examples" / "registry"
KEYGEN = shutil.which("ssh-keygen")
NOW = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc).timestamp()
QUIET = {"PATH": "/usr/bin:/bin", "LC_ALL": "C"}

BLUETOOTH = """id: io.github.ada.bluetooth
name: Bluetooth page
description: Opens the Bluetooth page of Settings
version: "{version}"
permissions: {{}}
actions:
  bluetooth.open:
    description: Open the Bluetooth settings, to pair headphones or a speaker
    open: {{ settings: Bluetooth }}
"""
TIDES = """id: io.github.ada.tides
name: Tides
description: Says when the next high tide is
version: "{version}"
permissions:
  network: [tides.example.org]
actions:
  tides.next:
    description: Say when the next high tide is at a seaside town
    params:
      town: {{ type: text, max: 40, description: "The town, like Brighton" }}
    required: [town]
    get: https://tides.example.org/v1/next?town={{town}}
    pick:
      time: [high, 0, time]
    say: "The next high tide in {{town}} is at {{time}}."
"""
TIDES_HOSTS = {"network": ["tides.example.org"]}


def make_key(folder: Path, name: str, kind: str = "ed25519", bits: int | None = None):
    """(private key file, "<type> <base64>") of a key made for this test run only."""
    argv = [KEYGEN, "-q", "-t", kind, "-N", "", "-C", "", "-f", str(folder / name)]
    subprocess.run(argv + (["-b", str(bits)] if bits else []), check=True,
                   stdin=subprocess.DEVNULL, env={**QUIET, "HOME": str(folder)})
    return folder / name, " ".join((folder / f"{name}.pub").read_text().split()[:2])


def sign(key: Path, namespace: str, data: bytes) -> bytes:
    return subprocess.run([KEYGEN, "-Y", "sign", "-q", "-f", str(key), "-n", namespace],
                          input=data, capture_output=True, check=True,
                          env={**QUIET, "HOME": str(key.parent)}).stdout


def fingerprint(public: str) -> str:
    return registry.key_fingerprint(public, "test key")


def dumped(data) -> bytes:
    """As REGISTRY-FORMAT.md has the registry write its files."""
    return (json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode()


@unittest.skipUnless(KEYGEN, "ssh-keygen isn't installed")
class RegistryCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.keys_dir = Path(tempfile.mkdtemp(prefix="tux-registry-keys-"))
        cls.addClassCleanup(shutil.rmtree, cls.keys_dir, True)
        for name in ("registry", "moderation", "ada", "eve", "other"):
            setattr(cls, name, make_key(cls.keys_dir, name))

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="tux-registry-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.state, self.skills = self.root / "state", self.root / "data" / "skills"
        # a switch-off held in memory is the daemon's, so it goes with the test's folders
        self.addCleanup(registry._UNWRITTEN.pop, self.skills.parent / registry.INSTALLED, None)
        self.skills.mkdir(parents=True)
        self.signers = self.root / "registry_allowed_signers"
        self.signers.write_text(
            f'tuxistant-registry namespaces="tuxistant-index" {self.registry[1]}\n')
        self.files: dict[str, bytes] = {}
        self.fetched: list[tuple[str, int]] = []
        self.changed = 0
        self.reg = self.new_registry()

    def new_registry(self) -> Registry:
        def changed():
            self.changed += 1
        return Registry(self.state, self.skills, fetch=self.fetch, signers=self.signers,
                        clock=lambda: NOW, on_change=changed)

    def fetch(self, path: str, limit: int) -> bytes:
        self.fetched.append((path, limit))
        if path not in self.files:
            raise OSError("not found")
        return self.files[path]

    # Building a registry

    def version(self, text: str, version: str, permissions=None, signer=None,
                namespace=RECIPE_NS, key=None) -> dict:
        data = text.format(version=version).encode()
        digest = hashlib.sha256(data).hexdigest()
        self.files[f"recipes/{digest}.yaml"] = data
        permissions = {} if permissions is None else permissions
        return {"version": version, "date": "2026-11-14T10:00:00Z", "sha256": digest,
                "size": len(data), "key": key or fingerprint(self.ada[1]),
                "sig": sign((signer or self.ada)[0], namespace, data).decode(),
                "permissions": permissions, "risk": "medium" if permissions else "low"}

    def skill(self, short: str, versions: list, **changes) -> dict:
        return {"id": f"io.github.ada.{short}", "author": "ada", "name": short.title(),
                "summary": f"The {short} skill", "tags": ["settings"], "licence": "MIT",
                "links": [], "versions": versions, **changes}

    def index(self, seq: int = 1, skills=None, revocations=(), lifted=(), **changes) -> dict:
        if skills is None:
            skills = [self.skill("bluetooth", [self.version(BLUETOOTH, "1.0.0")])]
        index = {"format": 1, "seq": seq, "date": "2026-11-20T09:00:00Z", "commit": "a" * 40,
                 "moderation_keys": [self.moderation[1]],
                 "categories": [{"id": "desktop", "name": "Desktop",
                                 "tags": ["settings", "apps"]}],
                 "authors": {"ada": {"status": "active", "keys": [
                     {"key": self.ada[1], "fingerprint": fingerprint(self.ada[1]),
                      "added": "2026-11-14", "removed": None}]}},
                 "skills": skills, "revocations": list(revocations), "lifted": list(lifted)}
        index.update(changes)
        return index

    def publish(self, index: dict, signer=None, namespace=INDEX_NS) -> bytes:
        data = dumped(index)
        self.files["index.json"] = data
        self.files["index.json.sig"] = sign((signer or self.registry)[0], namespace, data)
        return data

    def revoked(self, seq: int, entries, signer=None, namespace=REVOCATIONS_NS, **changes):
        found = {"format": 1, "seq": seq, "date": "2026-11-20T10:00:00Z",
                 "revocations": list(entries), **changes}
        data = dumped(found)
        self.files["revocations.json"] = data
        self.files["revocations.json.sig"] = sign((signer or self.moderation)[0], namespace, data)
        return data

    @staticmethod
    def entry(id: int, scope: dict, kind: str = "suspend", reason: str = "It went bad.") -> dict:
        return {"id": id, "kind": kind, "scope": scope, "reason": reason,
                "date": "2026-11-19T16:05:00Z", "actor": "mod",
                "report": "https://github.com/example/tuxistant-skills/issues/7"}

    @staticmethod
    def lift(id: int) -> int:
        """An index lists a lift by its id alone."""
        return id

    def accept(self, index: dict, **publish) -> dict:
        self.publish(index, **publish)
        return self.reg.accept_index(self.files["index.json"], self.files["index.json.sig"])

    def refused(self, call, *args, contains: str = "", **kwargs) -> str:
        with self.assertRaises(Refused) as caught:
            call(*args, **kwargs)
        self.assertIn(contains, str(caught.exception))
        return str(caught.exception)


class SignatureTests(RegistryCase):
    def check(self, data, sig, public=None, principal="p", namespace="ns"):
        registry.verify(data, sig, [f'{principal} namespaces="{namespace}" '
                                    f'{public or self.ada[1]}'],
                        principal, namespace, "That file")

    def test_a_good_signature_passes(self):
        self.check(b"hello\n", sign(self.ada[0], "ns", b"hello\n"))

    def test_changed_bytes_another_key_namespace_or_principal_are_refused(self):
        sig = sign(self.ada[0], "ns", b"hello\n")
        self.refused(self.check, b"hello!\n", sig, contains="isn't signed by")
        self.refused(self.check, b"hello\n", sig, self.eve[1])
        self.refused(self.check, b"hello\n", sign(self.ada[0], "other", b"hello\n"))
        with self.assertRaises(Refused):
            registry.verify(b"hello\n", sig, [f'p namespaces="ns" {self.ada[1]}'], "q", "ns", "It")

    def test_a_signature_that_isnt_armored_text_never_reaches_ssh_keygen(self):
        good = sign(self.ada[0], "ns", b"hello\n")
        with mock.patch.object(registry.subprocess, "run") as run:
            for sig in (b"", b"junk", good.replace(b"\n", b"\r\n"), good + b"x" * 4096,
                        good.decode()):
                self.refused(self.check, b"hello\n", sig, contains="without a signature")
            run.assert_not_called()

    def test_ssh_keygen_runs_with_a_fixed_argv_no_shell_a_timeout_and_private_files(self):
        seen = {}

        def run(argv, **kwargs):
            folder = Path(kwargs["cwd"])
            seen.update(argv=argv, kwargs=kwargs, mode=stat.S_IMODE(folder.stat().st_mode),
                        modes={p.name: stat.S_IMODE(p.stat().st_mode) for p in folder.iterdir()},
                        stdin=kwargs["stdin"].read())
            return subprocess.CompletedProcess(argv, 0)

        sig = sign(self.ada[0], "ns", b"hello\n")
        with mock.patch.object(registry.subprocess, "run", run):
            registry.verify(b"hello\n", sig, ["p x"], "p", "ns", "It", self.root / "work")
        argv, kwargs = seen["argv"], seen["kwargs"]
        self.assertTrue(Path(argv[0]).is_absolute())
        self.assertEqual(argv[1:3], ["-Y", "verify"])
        self.assertEqual([argv[5], argv[6], argv[7], argv[8]], ["-I", "p", "-n", "ns"])
        self.assertNotIn("shell", kwargs)
        self.assertEqual(kwargs["timeout"], registry.VERIFY_S)
        self.assertEqual(kwargs["env"]["PATH"], "/usr/bin:/bin")
        self.assertEqual(seen["stdin"], b"hello\n")
        self.assertEqual(seen["mode"], 0o700)
        self.assertEqual(set(seen["modes"].values()), {0o600})
        self.assertTrue(Path(kwargs["cwd"]).is_relative_to(self.root / "work"))
        self.assertFalse(Path(kwargs["cwd"]).exists())     # cleaned up
        self.assertEqual(stat.S_IMODE((self.root / "work").stat().st_mode), 0o700)

    def test_a_check_that_hangs_is_refused(self):
        sig = sign(self.ada[0], "ns", b"hello\n")
        hang = mock.Mock(side_effect=subprocess.TimeoutExpired(["ssh-keygen"], 10))
        with mock.patch.object(registry.subprocess, "run", hang):
            self.refused(self.check, b"hello\n", sig, contains="took too long")

    def test_without_ssh_keygen_it_says_so(self):
        with mock.patch.object(registry, "keygen", lambda: None):
            self.refused(self.check, b"x", sign(self.ada[0], "ns", b"x"),
                         contains="ssh-keygen isn't installed")

    def test_ssh_keygen_is_never_taken_from_the_callers_path(self):
        planted = self.root / "bin"
        planted.mkdir()
        (planted / "ssh-keygen").write_text("#!/bin/sh\nexit 0\n")
        (planted / "ssh-keygen").chmod(0o755)
        with mock.patch.dict(os.environ, {"PATH": f"{planted}:/usr/bin:/bin"}):
            self.assertNotEqual(Path(registry.keygen()).parent, planted)

    def test_fingerprints_match_ssh_keygen(self):
        rsa = make_key(self.root, "rsa", "rsa", 2048)
        ecdsa = make_key(self.root, "ecdsa", "ecdsa", 384)
        for name, public in (("ada", self.ada[1]), ("rsa", rsa[1]), ("ecdsa", ecdsa[1])):
            file = self.root / f"{name}.check.pub"
            file.write_text(public + "\n")
            listed = subprocess.run([KEYGEN, "-l", "-E", "sha256", "-f", str(file)],
                                    capture_output=True, text=True, check=True).stdout.split()[1]
            self.assertEqual(fingerprint(public), listed)

    def test_short_rsa_keys_and_mislabelled_keys_are_refused(self):
        short = make_key(self.root, "short", "rsa", 1024)
        self.refused(fingerprint, short[1])
        blob = self.ada[1].split()[1]
        self.refused(fingerprint, f"ssh-rsa {blob}")
        self.refused(fingerprint, f"ssh-dss {blob}")
        self.refused(fingerprint, f"ssh-ed25519 {blob[:-8]}")
        self.refused(fingerprint, f"ssh-ed25519 {blob} comment")

    def test_the_shipped_key_is_a_documented_placeholder_and_parses(self):
        text = registry.SHIPPED_SIGNERS.read_text()
        self.assertIn("PLACEHOLDER", text)
        self.assertEqual(len(registry.shipped_signers()), 1)

    def test_a_damaged_shipped_key_is_refused(self):
        line = self.signers.read_text().strip()
        for text in ("", line.replace("tuxistant-index", "tuxistant-recipe"),
                     line.replace("tuxistant-registry", "*"), f"{line}\n{line}\n{line}\n",
                     line.replace("ssh-ed25519", "ssh-rsa"), f'{line},valid-after="20261114Z"'):
            self.signers.write_text(text)
            self.refused(registry.shipped_signers, self.signers, contains="damaged")


class IndexTests(RegistryCase):
    def test_a_signed_index_is_accepted_and_kept(self):
        self.accept(self.index(seq=3))
        self.assertEqual(self.reg.index()["seq"], 3)
        again = self.new_registry()
        self.assertEqual(again.index()["seq"], 3)
        self.assertEqual(stat.S_IMODE(self.state.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((self.state / "state.json").stat().st_mode), 0o600)

    def test_another_key_or_namespace_is_refused(self):
        self.publish(self.index(), signer=self.eve)
        self.refused(self.reg.accept_index, self.files["index.json"],
                     self.files["index.json.sig"], contains="isn't signed")
        self.publish(self.index(), namespace=REVOCATIONS_NS)
        self.refused(self.reg.accept_index, self.files["index.json"],
                     self.files["index.json.sig"])
        self.publish(self.index(), signer=self.moderation, namespace=INDEX_NS)
        self.refused(self.reg.accept_index, self.files["index.json"],
                     self.files["index.json.sig"])
        # the registry key also signs skipped.yaml for the index build, which Tux never reads
        self.publish(self.index(), namespace="tuxistant-skip")
        self.refused(self.reg.accept_index, self.files["index.json"],
                     self.files["index.json.sig"], contains="isn't signed")
        self.assertIsNone(self.reg.index())

    def accept(self, index, **publish):     # signed by the registry key unless told otherwise
        publish.setdefault("signer", self.registry)
        return super().accept(index, **publish)

    def test_a_lower_sequence_is_refused_even_after_a_restart(self):
        self.accept(self.index(seq=5))
        self.refused(self.accept, self.index(seq=4), contains="older list of skills")
        self.reg = self.new_registry()
        self.refused(self.accept, self.index(seq=4), contains="older list of skills")
        self.assertEqual(self.reg.index()["seq"], 5)

    def test_the_same_sequence_needs_the_same_bytes(self):
        first = self.index(seq=5)
        self.accept(first)
        self.accept(first)
        self.refused(self.accept, self.index(seq=5, commit="b" * 40), contains="same number")
        self.assertEqual(self.reg.index()["commit"], "a" * 40)
        self.accept(self.index(seq=6, commit="b" * 40))
        self.assertEqual(self.reg.index()["seq"], 6)

    def test_the_state_is_written_whole_or_not_at_all(self):
        self.accept(self.index(seq=5))
        before = (self.state / "state.json").read_bytes()
        with mock.patch.object(registry.os, "fsync", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.accept(self.index(seq=6))
        self.assertEqual((self.state / "state.json").read_bytes(), before)
        self.assertEqual(self.reg.kept()["index"]["seq"], 5)
        self.assertEqual(sorted(p.name for p in self.state.iterdir()), ["index.json", "state.json"])
        self.reg = self.new_registry()
        self.assertEqual(self.reg.index()["seq"], 5)

    def test_nothing_is_taken_in_memory_that_wasnt_saved(self):
        self.accept(self.index(seq=5))
        real = registry._write

        def full(file, data, mode=0o600):
            if file.name == "state.json":
                raise OSError("disk full")
            real(file, data, mode)

        changed = self.index(seq=6, revocations=[self.entry(1, {"author": "ada"})],
                             authors={"ada": {**self.index()["authors"]["ada"],
                                              "status": "suspended"}})
        with mock.patch.object(registry, "_write", full), self.assertRaises(OSError):
            self.accept(changed)
        self.assertEqual(self.reg.kept()["index"]["seq"], 5)
        self.assertEqual(self.reg.in_effect(), [])

    def test_a_state_that_cant_be_read_refuses_everything_until_it_is_fixed(self):
        self.accept(self.index(seq=5))
        file = self.state / "state.json"
        good = file.read_bytes()
        damaged = (b"x" * (registry.MAX_STATE_BYTES + 1), b"{not json", b"[]",
                   good.replace(b'"listed": {}', b'"listed": {"1": 1}'),
                   good.replace(b'"moderated": {}', b'"moderated": {"k": {}}'),
                   good.replace(b'"lifted": []', b'"lifted": ["1"]'),
                   good.replace(b'"seq": 5', b'"seq": "5"'))
        for data in damaged:
            with self.subTest(data=data[:40]):
                self.assertNotEqual(data, good)
                file.write_bytes(data)
                self.reg = self.new_registry()
                self.refused(self.accept, self.index(seq=1), contains="damaged")
                self.refused(self.accept, self.index(seq=6), contains="damaged")
                self.revoked(1, [])
                self.refused(self.reg.accept_revocations, self.files["revocations.json"],
                             self.files["revocations.json.sig"], contains="damaged")
                self.refused(self.reg.check, "io.github.ada.bluetooth", "1.0.0",
                             contains="damaged")
                self.assertEqual(self.reg.refresh(), [])
                self.assertIn("damaged", self.reg.problems["state"])
                self.assertEqual(file.read_bytes(), data)
        file.write_bytes(good)
        self.reg = self.new_registry()
        self.refused(self.accept, self.index(seq=1), contains="older list of skills")

    def test_the_state_never_grows_past_what_it_can_read_back(self):
        self.accept(self.index(seq=5))
        size = len((self.state / "state.json").read_bytes())
        n = 0

        def grow(seq):
            nonlocal n
            n += 10
            self.revoked(seq, [self.entry(id, {"skill": "io.github.ada.nothing"},
                                          reason="x" * 300) for id in range(n - 9, n + 1)])
            return self.reg.accept_revocations(self.files["revocations.json"],
                                               self.files["revocations.json.sig"])

        with mock.patch.object(registry, "MAX_STATE_BYTES", size + 20000):
            seq = 1
            while True:
                try:
                    grow(seq)
                except Refused as e:
                    self.assertIn("more revoked skills", str(e))
                    break
                seq += 1
                self.assertLess(seq, 100)
            self.assertLessEqual(len((self.state / "state.json").read_bytes()),
                                 registry.MAX_STATE_BYTES)
            self.reg = self.new_registry()
            self.assertEqual(self.reg.kept()["index"]["seq"], 5)
            self.assertEqual(len(self.reg.in_effect()), 10 * (seq - 1))
            self.refused(self.accept, self.index(seq=1), contains="older list of skills")

    def test_a_damaged_state_names_the_folder_it_is_really_in(self):
        """XDG_STATE_HOME moves it, so the sentence can't name ~/.local/state."""
        self.accept(self.index())
        (self.state / "state.json").write_bytes(b"{not json")
        message = self.refused(self.new_registry().kept, contains="damaged")
        self.assertIn(f"Removing state.json in {registry._shown(self.state)} starts it again.",
                      message)
        with mock.patch.object(registry.Path, "home", return_value=self.root):
            self.refused(self.new_registry().kept, contains="Removing state.json in ~/state ")

    def test_an_index_with_more_revocations_in_effect_than_tux_keeps_breaks_the_format(self):
        """A rule for the registry, so it can't publish an index every Tux would refuse."""
        entries = [self.entry(n, {"skill": "io.github.zz.x"})
                   for n in range(1, registry.MAX_LIVE + 2)]
        self.refused(self.accept, self.index(revocations=entries), contains="format")
        self.refused(self.accept, self.index(revocations=entries, lifted=[self.lift(1)]),
                     contains="format")
        self.accept(self.index(revocations=entries[1:], lifted=[self.lift(1)]))
        self.assertEqual(len(self.reg.in_effect()), registry.MAX_LIVE)
        lifts = [self.lift(n) for n in range(1, registry.MAX_LIFTED + 2)]
        self.refused(self.accept, self.index(seq=2, lifted=lifts), contains="format")
        self.accept(self.index(seq=2, lifted=lifts[:-1]))

    def test_the_most_the_registry_can_publish_is_taken(self):
        """Every limit at once, every field at its longest, written as the format says: both
        files fit what Tux reads, and the revocations and lifts leave 3.5 MiB of the index."""
        report = ("https://github.com/" + "a" * 39 + "/" + "r" * 100 + "/issues/"
                  + "9" * 10)
        top = 2 ** 53

        def longest(id):
            return {"id": id, "kind": "suspend", "reason": "\U0001F600" * 300,
                    "date": "2026-11-19T16:05:00Z", "actor": "a" * 39, "report": report,
                    "scope": {"skill": "io.github." + "a" * 39 + "." + "s" * 40,
                              "versions": [f"{m:02d}" + "v" * 38 for m in range(50)]}}

        live = [longest(id) for id in range(top - registry.MAX_LIVE + 1, top + 1)]
        floor = top - registry.MAX_LIVE
        lifts = list(range(floor - registry.MAX_LIFTED + 1, floor + 1))
        index = self.index(seq=top, revocations=live, lifted=lifts)
        part = dumped({"revocations": live, "lifted": lifts})
        self.assertLess(len(part), registry.MAX_INDEX_BYTES - 3.5 * 1024 * 1024)
        self.assertEqual(self.accept(index)["seq"], top)
        self.assertEqual(len(self.reg.in_effect()), registry.MAX_LIVE)
        # a revocations.json of the most in effect, ids the index hasn't seen yet
        floor -= registry.MAX_LIFTED
        found = [longest(id) for id in range(floor - registry.MAX_LIVE + 1, floor + 1)]
        data = self.revoked(top, found)
        self.assertLess(len(data), 4.5 * 1024 * 1024)
        self.reg.accept_revocations(data, self.files["revocations.json.sig"])
        self.assertEqual(len(self.new_registry().in_effect()), 2 * registry.MAX_LIVE)

    def test_a_held_copy_that_isnt_the_one_the_state_names_isnt_used(self):
        self.accept(self.index(seq=5))
        (self.state / "index.json").write_bytes(dumped(self.index(seq=9)))
        self.assertIsNone(self.new_registry().index())

    def test_a_newer_format_asks_for_a_newer_tuxistant(self):
        self.refused(self.accept, self.index(format=2), contains="newer Tuxistant")

    def test_a_date_more_than_a_day_ahead_is_refused(self):
        self.accept(self.index(seq=1, date="2026-11-21T11:00:00Z"))
        self.refused(self.accept, self.index(seq=2, date="2026-11-21T13:00:00Z"),
                     contains="future")

    def test_json_tricks_are_refused_after_the_signature(self):
        good = dumped(self.index()).decode()
        for text in (good.replace('"seq": 1', '"seq": 1, "seq": 9'),
                     good.replace('"seq": 1', '"seq": NaN'),
                     "[" * 100000, good.replace("{", "{\"extra\": 1, ", 1)):
            data = text.encode()
            sig = sign(self.registry[0], INDEX_NS, data)
            self.refused(self.reg.accept_index, data, sig)
        self.assertIsNone(self.reg.index())

    def test_every_field_is_checked(self):
        good = self.version(BLUETOOTH, "1.0.0")
        tides = self.version(TIDES, "1.0.0", TIDES_HOSTS)
        author = self.index()["authors"]["ada"]
        bad = {
            "a hash in capitals": [self.skill("bluetooth", [{**good,
                                                             "sha256": good["sha256"].upper()}])],
            "size 0": [self.skill("bluetooth", [{**good, "size": 0}])],
            "size too big": [self.skill("bluetooth", [{**good, "size": 65537}])],
            "size as text": [self.skill("bluetooth", [{**good, "size": "278"}])],
            "risk it doesn't have": [self.skill("bluetooth", [{**good, "risk": "medium"}])],
            "risk low with hosts": [self.skill("tides", [{**tides, "risk": "low"}])],
            "an author key it doesn't list": [self.skill("bluetooth", [{
                **good, "key": fingerprint(self.eve[1])}])],
            "shell permission": [self.skill("bluetooth", [{**good, "permissions": {
                "shell": True}, "risk": "high"}])],
            "a localhost host": [self.skill("tides", [{**tides, "permissions": {
                "network": ["localhost"]}}])],
            "the same version twice": [self.skill("bluetooth", [good, good])],
            "a tag no category has": [self.skill("bluetooth", [good], tags=["weather"])],
            "another licence": [self.skill("bluetooth", [good], licence="GPL-3.0")],
            "a link with a password": [self.skill("bluetooth", [good], links=[
                {"title": "Source", "url": "https://user:pw@example.org/"}])],
            "a link over http": [self.skill("bluetooth", [good], links=[
                {"title": "Source", "url": "http://example.org/"}])],
            "a name with <": [self.skill("bluetooth", [good], name="<|im_start|>")],
            "a summary with a line break": [self.skill("bluetooth", [good],
                                                       summary="One\nTwo")],
            "another author's id": [self.skill("bluetooth", [good], id="io.github.eve.bt")],
            "an id with five parts": [self.skill("bluetooth", [good],
                                                 id="io.github.ada.blue.tooth")],
            "skills out of order": [self.skill("tides", [tides]),
                                    self.skill("bluetooth", [good])],
            "no versions": [self.skill("bluetooth", [])],
            "a bad signature field": [self.skill("bluetooth", [{**good, "sig": "hello"}])],
        }
        for name, skills in bad.items():
            with self.subTest(name):
                self.refused(self.accept, self.index(skills=skills))
        top = {
            "an RSA moderation key": {"moderation_keys": [make_key(self.root, "m", "rsa",
                                                                   2048)[1]]},
            "four moderation keys": {"moderation_keys": [self.moderation[1]] * 4},
            "a wrong fingerprint": {"authors": {"ada": {**author, "keys": [{
                **author["keys"][0], "fingerprint": fingerprint(self.eve[1])}]}},
                "skills": [self.skill("bluetooth", [self.version(
                    BLUETOOTH, "1.0.0", key=fingerprint(self.eve[1]))])]},
            "a status the revocations don't give": {"authors": {"ada": {**author,
                                                                         "status": "banned"}}},
            "a bad day": {"authors": {"ada": {**author, "keys": [{
                **author["keys"][0], "added": "2026-02-30"}]}}},
            "a tag in two categories": {"categories": [
                {"id": "a1", "name": "A", "tags": ["settings"]},
                {"id": "b1", "name": "B", "tags": ["settings"]}]},
            "a short commit": {"commit": "a" * 39},
            "a timestamp with an offset": {"date": "2026-11-20T09:00:00+00:00"},
            "a sequence of 0": {"seq": 0},
            "a sequence that is true": {"seq": True},
            "a ban scoped to a skill": {"revocations": [self.entry(
                1, {"skill": "io.github.ada.bluetooth"}, "ban")]},
            "a revocation with a report elsewhere": {"revocations": [{**self.entry(
                1, {"author": "ada"}), "report": "https://example.org/issues/7"}]},
            "revocations out of order": {"revocations": [
                self.entry(2, {"key": fingerprint(self.eve[1])}),
                self.entry(1, {"key": fingerprint(self.eve[1])})]},
            "a lift as an entry": {"lifted": [{"id": 1}]},
            "a lift that is true": {"lifted": [True]},
            "a lift of 0": {"lifted": [0]},
            "lifts out of order": {"lifted": [2, 1]},
            "a lift twice": {"lifted": [1, 1]},
            "a lifted revocation still listed": {"revocations": [self.entry(
                1, {"key": fingerprint(self.eve[1])})], "lifted": [1]},
        }
        for name, changes in top.items():
            with self.subTest(name):
                self.refused(self.accept, self.index(**changes))
        self.assertIsNone(self.reg.index())
        banned = self.index(revocations=[self.entry(1, {"author": "ada"}, "ban")],
                            authors={"ada": {**author, "status": "banned"}})
        self.accept(banned)

    def logo(self, **changes) -> dict:
        data = b"never fetched, so never decoded"
        return {"type": "webp", "sha256": hashlib.sha256(data).hexdigest(), "size": 4096,
                "width": 256, "height": 256, "frames": 1, "duration_ms": 0,
                "key": fingerprint(self.ada[1]),
                "sig": sign(self.ada[0], "tuxistant-logo", data).decode(),
                "still_sha256": "b" * 64, **changes}

    def test_a_logo_is_taken_as_the_format_gives_it_and_never_fetched(self):
        still, animated = self.version(BLUETOOTH, "1.0.0"), self.version(BLUETOOTH, "1.1.0")
        longest = self.logo(type="gif", frames=120, duration_ms=8000, size=512 * 1024,
                            width=512, height=512)
        self.accept(self.index(skills=[self.skill("bluetooth", [
            {**still, "logo": self.logo(width=128, height=128, size=256 * 1024)},
            {**animated, "logo": longest}])]))
        self.fetched.clear()
        self.reg.check("io.github.ada.bluetooth", "1.1.0")
        self.assertEqual(self.fetched, [(f"recipes/{animated['sha256']}.yaml", animated["size"])])
        self.accept(self.index(seq=2, skills=[self.skill("bluetooth", [{**still, "logo": None}])]))

    def test_a_logo_out_of_the_format_refuses_the_index(self):
        good = self.version(BLUETOOTH, "1.0.0")
        bad = {
            "an SVG": self.logo(type="svg"),
            "not square": self.logo(height=255),
            "too small": self.logo(width=64, height=64),
            "too big": self.logo(width=1024, height=1024),
            "a still over 256 KiB": self.logo(size=256 * 1024 + 1),
            "an animation over 512 KiB": self.logo(frames=2, duration_ms=100,
                                                   size=512 * 1024 + 1),
            "a still with a duration": self.logo(duration_ms=40),
            "frames under 20 ms": self.logo(frames=10, duration_ms=199),
            "a loop over 8 seconds": self.logo(frames=10, duration_ms=8001),
            "121 frames": self.logo(frames=121, duration_ms=8000),
            "a key its author doesn't list": self.logo(key=fingerprint(self.eve[1])),
            "a hash in capitals": self.logo(still_sha256="B" * 64),
            "no still": {k: v for k, v in self.logo().items() if k != "still_sha256"},
            "the image itself": {**self.logo(), "data": "UklGRg=="},
            "a size as text": self.logo(size="4096"),
            "a bad signature": self.logo(sig="hello"),
            "a file name": "logo.png",
        }
        for name, logo in bad.items():
            with self.subTest(name):
                self.refused(self.accept, self.index(skills=[self.skill("bluetooth", [
                    {**good, "logo": logo}])]), contains="logo")
        self.assertIsNone(self.reg.index())

    def test_refresh_keeps_the_index_it_had_and_says_why(self):
        self.publish(self.index(seq=2))
        self.reg.refresh()
        self.assertEqual(self.reg.index()["seq"], 2)
        self.publish(self.index(seq=1))
        self.reg.refresh()
        self.assertEqual(self.reg.index()["seq"], 2)
        self.assertIn("older list", self.reg.problems["index"])
        self.files.clear()
        self.reg.refresh()
        self.assertEqual(self.reg.problems["index"], registry.NOT_REACHED)
        self.assertIn(("index.json", registry.MAX_INDEX_BYTES), self.fetched)
        self.assertIn(("index.json.sig", registry.MAX_SIG_BYTES), self.fetched)

    def test_a_fetch_that_sends_more_than_it_was_allowed_is_refused(self):
        self.publish(self.index())
        self.reg.fetch = lambda path, limit: self.files[path] + b" " * limit
        self.reg.refresh()
        self.assertIn("too big", self.reg.problems["index"])
        self.assertIsNone(self.reg.index())

    def test_without_a_fetch_nothing_is_reached(self):
        reg = Registry(self.state, self.skills, signers=self.signers)
        reg.refresh()
        self.assertIn("can't fetch", reg.problems["index"])

    def test_the_signed_example_in_the_docs_checks_out(self):
        """The format page and this code agree on every field of a real signed index."""
        reg = Registry(self.state, self.skills, fetch=lambda p, _l: (EXAMPLE / p).read_bytes(),
                       signers=EXAMPLE / "registry_allowed_signers", clock=lambda: NOW)
        self.assertEqual(reg.refresh(), [])
        self.assertEqual(reg.problems, {})
        self.assertEqual(reg.index()["seq"], 4)
        self.assertEqual([entry["id"] for entry in reg.in_effect()], [1])
        checked = reg.check("io.github.example-ada.tides", "1.1.0")
        self.assertEqual(checked.manifest["permissions"], {"network": ["tides.example.org"]})
        reg.check("io.github.example-bo.bluetooth", "1.0.0")
        self.refused(reg.check, "io.github.example-ada.tides", "1.0.0", contains="taken over")


class RevocationTests(RegistryCase):
    def setUp(self):
        super().setUp()
        self.accept(self.index(seq=1))

    def take(self, seq, entries, **kwargs):
        self.revoked(seq, entries, **kwargs)
        return self.reg.accept_revocations(self.files["revocations.json"],
                                           self.files["revocations.json.sig"])

    def test_the_moderation_key_named_by_the_index_signs_them(self):
        self.take(1, [self.entry(1, {"skill": "io.github.ada.bluetooth"})])
        self.assertEqual([entry["id"] for entry in self.reg.in_effect()], [1])

    def test_no_other_key_or_namespace_will_do(self):
        entries = [self.entry(1, {"skill": "io.github.ada.bluetooth"})]
        for signer, namespace in ((self.registry, REVOCATIONS_NS), (self.eve, REVOCATIONS_NS),
                                  (self.moderation, INDEX_NS), (self.ada, RECIPE_NS),
                                  (self.moderation, "git")):
            with self.subTest(signer=signer[0].name, namespace=namespace):
                self.refused(self.take, 1, entries, signer=signer, namespace=namespace)
        self.assertEqual(self.reg.in_effect(), [])

    def test_the_moderation_key_cant_sign_an_index(self):
        self.refused(self.accept, self.index(seq=2), signer=self.moderation)

    def test_without_an_index_nothing_is_taken(self):
        shutil.rmtree(self.state)
        self.reg = self.new_registry()
        self.refused(self.take, 1, [self.entry(1, {"author": "ada"})], contains="no list")

    def test_one_bad_entry_or_an_extra_field_refuses_the_whole_file(self):
        good = self.entry(1, {"author": "ada"})
        self.refused(self.take, 1, [good, self.entry(2, {"skill": "io.github.ada.bluetooth"},
                                                     "ban")])
        self.refused(self.take, 1, [good], lifted=[self.lift(1)])
        self.refused(self.take, 1, [good, {**self.entry(2, {"author": "ada"}), "note": "x"}])
        self.refused(self.take, 1, [good, self.entry(2, {"author": "ada", "key": "x"})])
        self.assertEqual(self.reg.in_effect(), [])

    def test_a_lower_sequence_is_refused(self):
        self.take(3, [self.entry(1, {"author": "ada"})])
        self.refused(self.take, 2, [], contains="older list of revoked")
        self.refused(self.take, 3, [], contains="same number")

    def test_every_copy_of_an_id_is_in_effect_and_a_left_out_entry_stays(self):
        wide = self.entry(1, {"skill": "io.github.ada.bluetooth"})
        self.take(1, [wide])
        narrowed = self.entry(1, {"skill": "io.github.ada.bluetooth", "versions": ["0.1"]})
        self.take(2, [narrowed])
        self.assertCountEqual([entry["scope"] for entry in self.reg.in_effect()],
                              [wide["scope"], narrowed["scope"]])
        self.assertTrue(self.reg.covering("io.github.ada.bluetooth", "1.0.0", "k"))
        self.take(3, [])
        self.assertEqual([entry["id"] for entry in self.reg.in_effect()], [1, 1])
        self.assertCountEqual([entry["scope"] for entry in self.new_registry().in_effect()],
                              [wide["scope"], narrowed["scope"]])

    def test_a_copy_the_moderation_key_claimed_first_doesnt_hide_the_registrys(self):
        # a leaked moderation key signing future ids for nothing at all
        self.take(1, [self.entry(n, {"skill": "io.github.ada.nothing", "versions": ["9"]})
                      for n in (2, 3, 4)])
        ban = self.entry(2, {"author": "ada"}, "ban", "Malware.")
        self.accept(self.index(seq=2, revocations=[ban],
                               authors={"ada": {**self.index()["authors"]["ada"],
                                                "status": "banned"}}))
        self.assertEqual(self.reg.covering("io.github.ada.bluetooth", "1.0.0", "k"), ban)
        self.assertEqual(self.new_registry().covering("io.github.ada.bluetooth", "1.0.0", "k"),
                         ban)

    def test_a_lifted_id_is_forgotten_with_every_copy(self):
        self.take(1, [self.entry(1, {"author": "ada"}), self.entry(2, {"author": "ada"})])
        self.accept(self.index(seq=2, lifted=[self.lift(1)]))
        self.assertEqual([entry["id"] for entry in self.reg.in_effect()], [2])

    def test_no_more_moderation_copies_than_the_state_can_hold_and_a_lift_makes_room(self):
        with mock.patch.object(registry, "MAX_MODERATED", 3):
            self.take(1, [self.entry(n, {"author": "ada"}) for n in (1, 2, 3)])
            self.refused(self.take, 2, [self.entry(4, {"author": "ada"})],
                         contains="more revoked skills")
            self.assertEqual([entry["id"] for entry in self.reg.in_effect()], [1, 2, 3])
            self.accept(self.index(seq=2, lifted=[self.lift(1)]))
            self.take(2, [self.entry(4, {"author": "ada"})])
            self.assertEqual([entry["id"] for entry in self.reg.in_effect()], [2, 3, 4])
            with mock.patch.object(registry, "MAX_LIFTED", 2):
                self.accept(self.index(seq=3, lifted=[self.lift(1), self.lift(2)]))
                held = self.reg.kept()["lifted"]
                self.refused(self.accept, self.index(seq=4, lifted=[self.lift(1), self.lift(5)]),
                             contains="more revoked skills")
                self.assertEqual(self.reg.kept()["lifted"], held)
            self.assertEqual(self.reg.index()["seq"], 3)

    def test_a_copy_the_index_lists_doesnt_count_against_the_moderation_share(self):
        """The moderation workflow lists every revocation ever made, so the ones an index
        already carries mustn't fill the share for the ones it doesn't."""
        two = [self.entry(n, {"skill": "io.github.ada.nothing"}) for n in (1, 2)]
        self.accept(self.index(seq=2, revocations=two))
        with mock.patch.object(registry, "MAX_MODERATED", 1):
            self.take(1, two + [self.entry(3, {"skill": "io.github.ada.nothing"})])
            self.assertEqual([entry["id"] for entry in self.reg.in_effect()], [1, 2, 3])
            self.assertEqual(sum(map(len, self.reg.kept()["moderated"].values())), 1)

    def test_the_most_the_state_can_hold_fits_in_what_tux_reads_back(self):
        report = ("https://github.com/" + "a" * 39 + "/" + "r" * 100 + "/issues/"
                  + "9" * 10)
        worst = {"kind": "suspend", "reason": "\U0001F600" * 300,
                 "date": "2026-11-19T16:05:00Z", "actor": "a" * 39, "report": report}
        top = 2 ** 53

        def copies(ids):
            out = {}
            for n in ids:
                entry = {**worst, "id": n,
                         "scope": {"skill": "io.github." + "a" * 39 + "." + "s" * 40,
                                   "versions": [f"{m:02d}" + "v" * 38 for m in range(50)]}}
                out[registry._copy_key(registry.revocation(entry))] = entry
            return out

        listed = copies(range(top - registry.MAX_LISTED + 1, top + 1))
        keys = [self.moderation[1], self.eve[1], self.other[1]]
        floor = top - registry.MAX_LISTED - registry.MAX_MODERATED
        ids = list(range(floor + 1, floor + registry.MAX_MODERATED + 1))
        third = len(ids) // 3
        moderated = {keys[0]: copies(ids[:third]), keys[1]: copies(ids[third:2 * third]),
                     keys[2]: copies(ids[2 * third:])}
        held = {"seq": top, "sha256": "f" * 64}
        state = {"index": held, "revocations": {**held, "key": keys[0]}, "listed": listed,
                 "moderated": moderated,
                 "lifted": list(range(floor - registry.MAX_LIFTED + 1, floor + 1))}
        data = registry._dumps(state)
        self.assertLessEqual(len(data), registry.MAX_STATE_BYTES)
        registry._private_folder(self.state)
        (self.state / "state.json").write_bytes(data)
        kept = self.new_registry().kept()
        self.assertEqual(len(kept["listed"]), registry.MAX_LISTED)
        self.assertEqual(sum(map(len, kept["moderated"].values())), registry.MAX_MODERATED)

    def test_only_an_index_lifts_and_a_lifted_id_stays_lifted(self):
        self.take(1, [self.entry(1, {"skill": "io.github.ada.bluetooth"})])
        self.accept(self.index(seq=2, lifted=[self.lift(1)]))
        self.assertEqual(self.reg.in_effect(), [])
        self.take(2, [self.entry(1, {"author": "ada"})])
        self.accept(self.index(seq=3))
        self.assertEqual(self.reg.in_effect(), [])

    def test_the_index_carries_revocations_too(self):
        self.accept(self.index(seq=2, revocations=[self.entry(4, {"author": "ada"})],
                               authors={"ada": {**self.index()["authors"]["ada"],
                                                "status": "suspended"}}))
        self.assertEqual([entry["id"] for entry in self.reg.in_effect()], [4])

    def test_what_a_scope_covers(self):
        key = fingerprint(self.ada[1])
        cases = [({"skill": "io.github.ada.tides"}, "io.github.ada.tides", "2.0", True),
                 ({"skill": "io.github.ada.tides"}, "io.github.ada.tidesx", "2.0", False),
                 ({"skill": "io.github.ada.tides", "versions": ["1.0"]},
                  "io.github.ada.tides", "2.0", False),
                 ({"skill": "io.github.ada.tides", "versions": ["1.0"]},
                  "io.github.ada.tides", "1.0", True),
                 ({"author": "ada"}, "io.github.ada.tides", "1.0", True),
                 ({"author": "ad"}, "io.github.ada.tides", "1.0", False),
                 ({"key": key}, "io.github.bo.x", "1.0", True),
                 ({"key": fingerprint(self.eve[1])}, "io.github.ada.tides", "1.0", False)]
        for scope, skill_id, version, expected in cases:
            self.assertEqual(registry.covers({"scope": scope}, skill_id, version, key), expected,
                             (scope, skill_id, version))


class LeakedModerationKeyTests(RegistryCase):
    """A leaked moderation key can switch skills off and nothing more. A review found it could
    fill the copies Tux keeps so that every later index was refused, the registry's new bans and
    the index retiring the key among them, and raise its sequence number out of reach of every
    later moderator."""

    def setUp(self):
        super().setUp()
        self.base = self.index(skills=[self.skill("bluetooth", [
            self.version(BLUETOOTH, "1.0.0"), self.version(BLUETOOTH, "1.1.0")])])
        self.accept(self.base)
        self.file = self.reg.install(self.reg.check("io.github.ada.bluetooth", "1.0.0"))

    def take(self, seq, entries, signer=None):
        self.revoked(seq, entries, signer=signer)
        return self.reg.accept_revocations(self.files["revocations.json"],
                                           self.files["revocations.json.sig"])

    def junk(self, seq, count):
        """Ids the registry never used and can't guess, for a skill nobody has."""
        return self.take(seq, [self.entry(10 ** 12 + n, {"skill": "io.github.zz.x"})
                               for n in range(count)])

    def later(self, seq, moderation_keys=None, **changes):
        return self.index(seq=seq, skills=self.base["skills"],
                          moderation_keys=moderation_keys or [self.moderation[1]], **changes)

    def ban(self, seq, moderation_keys=None):
        return self.later(seq, moderation_keys,
                          authors={"ada": {**self.base["authors"]["ada"], "status": "banned"}},
                          revocations=[self.entry(1, {"author": "ada"}, "ban", "Malware.")])

    def test_a_full_moderation_share_never_stops_the_registrys_index(self):
        self.junk(1, registry.MAX_MODERATED)
        self.refused(self.junk, 2, registry.MAX_MODERATED + 1, contains="more revoked skills")
        self.publish(self.ban(2))
        changes = self.reg.refresh()
        self.assertNotIn("index", self.reg.problems)
        self.assertEqual(self.reg.index()["seq"], 2)
        self.assertEqual([(change["off"], change["kind"]) for change in changes], [(True, "ban")])
        self.refused(self.reg.check, "io.github.ada.bluetooth", "1.0.0", contains="for good")

    def test_the_index_that_retires_a_key_takes_the_copies_only_it_signed(self):
        self.junk(1, registry.MAX_MODERATED)
        self.accept(self.ban(2, [self.other[1]]))
        self.assertEqual([entry["id"] for entry in self.reg.in_effect()], [1])
        self.assertEqual([(change["off"], change["kind"]) for change in self.reg.apply()],
                         [(True, "ban")])
        self.take(1, [self.entry(2, {"skill": "io.github.ada.other"})], signer=self.other)
        self.assertEqual([entry["id"] for entry in self.reg.in_effect()], [1, 2])
        self.refused(self.junk, 3, 1, contains="isn't signed")
        self.assertEqual([entry["id"] for entry in self.new_registry().in_effect()], [1, 2])

    def test_a_key_the_index_still_names_keeps_its_copies_and_its_number(self):
        self.take(4, [self.entry(2, {"skill": "io.github.ada.other"})])
        self.accept(self.later(2, [self.moderation[1], self.other[1]]))
        self.assertEqual([entry["id"] for entry in self.reg.in_effect()], [2])
        self.refused(self.take, 3, [], signer=self.other, contains="older list of revoked")

    def test_a_number_raised_out_of_reach_ends_with_the_key(self):
        self.take(2 ** 53, [])
        self.accept(self.later(2, [self.other[1]]))
        self.take(5, [self.entry(1, {"author": "ada"}, reason="Gone bad.")], signer=self.other)
        self.assertEqual([(change["off"], change["reason"]) for change in self.reg.apply()],
                         [(True, "Gone bad.")])
        self.refused(self.take, 4, [], signer=self.other, contains="older list of revoked")

    def test_a_switch_off_by_a_retired_key_stays_until_it_is_lifted(self):
        self.take(1, [self.entry(7, {"author": "ada"}, reason="Junk.")])
        self.reg.apply()
        self.accept(self.later(2, [self.other[1]]))
        self.assertEqual(self.reg.in_effect(), [])
        self.assertEqual(self.reg.apply(), [])
        self.assertIn(self.file.name, registry.switched_off(self.skills))
        self.refused(self.reg.install, self.reg.check("io.github.ada.bluetooth", "1.1.0"),
                     contains="Junk.")
        self.accept(self.later(3, [self.other[1]], lifted=[self.lift(7)]))
        self.assertEqual([change["off"] for change in self.reg.apply()], [False])


class RecipeTests(RegistryCase):
    def setUp(self):
        super().setUp()
        self.good = self.version(BLUETOOTH, "1.0.0")
        self.tides = self.version(TIDES, "1.0.0", TIDES_HOSTS)

    def with_versions(self, *versions, name="bluetooth"):
        others = [self.skill("tides", [self.tides])] if name == "bluetooth" else []
        self.accept(self.index(skills=[self.skill(name, list(versions))] + others))

    def test_a_good_recipe_is_checked_and_loaded(self):
        self.with_versions(self.good)
        checked = self.reg.check("io.github.ada.bluetooth", "1.0.0")
        self.assertEqual(checked.manifest["id"], "io.github.ada.bluetooth")
        self.assertEqual(checked.data, BLUETOOTH.format(version="1.0.0").encode())
        self.assertIn((f"recipes/{self.good['sha256']}.yaml", self.good["size"]), self.fetched)
        tides = self.reg.check("io.github.ada.tides", "1.0.0")
        self.assertEqual(tides.manifest["permissions"], TIDES_HOSTS)

    def test_other_bytes_are_refused(self):
        self.with_versions(self.good)
        path = f"recipes/{self.good['sha256']}.yaml"
        original = self.files[path]
        for data, why in ((original.replace(b"Bluetooth page", b"Bluetooth PAGE"), "isn't the one"),
                          (original[:-1], "isn't the one"), (original + b"\n", "too big")):
            self.files[path] = data
            self.refused(self.reg.check, "io.github.ada.bluetooth", "1.0.0", contains=why)

    def test_a_signature_from_another_key_or_namespace_is_refused(self):
        for version in (self.version(BLUETOOTH, "1.0.0", signer=self.eve),
                        self.version(BLUETOOTH, "1.0.0", namespace=INDEX_NS),
                        self.version(BLUETOOTH, "1.0.0", namespace="git"),
                        # what else a publisher's key signs: a logo, and allowed_signers
                        self.version(BLUETOOTH, "1.0.0", namespace="tuxistant-logo"),
                        self.version(BLUETOOTH, "1.0.0", namespace="tuxistant-keys")):
            self.with_versions(version)
            self.refused(self.reg.check, "io.github.ada.bluetooth", "1.0.0",
                         contains="isn't signed")
            shutil.rmtree(self.state)
            self.reg = self.new_registry()

    def test_the_principal_is_the_author(self):
        """Eve's key, listed for Eve, can't sign a recipe Ada's index entry names as Ada's."""
        data = BLUETOOTH.format(version="1.0.0").encode()
        sig = sign(self.eve[0], RECIPE_NS, data)
        allowed = [f'io.github.eve namespaces="{RECIPE_NS}" {self.eve[1]}']
        registry.verify(data, sig, allowed, "io.github.eve", RECIPE_NS, "It")
        self.refused(registry.verify, data, sig, allowed, "io.github.ada", RECIPE_NS, "It")

    def test_the_loader_has_the_last_word(self):
        for text, version, hosts, why in (
                (BLUETOOTH.replace("version:", "run: [sh]\nversion:"), "1.0.0", None,
                 "can't load"),
                (BLUETOOTH, "1.0.1", None, None),     # loads as 1.0.1, listed as 1.0.0
                (TIDES, "1.0.0", None, None),         # names a host the list doesn't
                (BLUETOOTH.replace("ada.bluetooth", "ada.other"), "1.0.0", None, None)):
            entry = self.version(text, version, hosts)
            entry["version"] = "1.0.0"
            self.with_versions(entry)
            message = self.refused(self.reg.check, "io.github.ada.bluetooth", "1.0.0")
            self.assertIn(why or "isn't what the skills registry's list says", message)
            shutil.rmtree(self.state)
            self.reg = self.new_registry()

    def test_a_revoked_version_isnt_downloaded(self):
        self.with_versions(self.good)
        self.revoked(1, [self.entry(1, {"skill": "io.github.ada.bluetooth",
                                        "versions": ["1.0.0"]}, reason="Its host was lost.")])
        self.reg.refresh()
        self.fetched.clear()
        self.refused(self.reg.check, "io.github.ada.bluetooth", "1.0.0",
                     contains="Its host was lost.")
        self.assertEqual(self.fetched, [])

    def test_an_unlisted_skill_or_version(self):
        self.with_versions(self.good)
        self.refused(self.reg.check, "io.github.ada.bluetooth", "2.0", contains="doesn't list")
        self.refused(self.reg.check, "io.github.ada.nope", "1.0.0", contains="doesn't list")


class InstalledTests(RegistryCase):
    """Revocations reaching recipes already installed, and the loader switching them off."""

    def setUp(self):
        super().setUp()
        self.v1 = self.version(BLUETOOTH, "1.0.0")
        self.v2 = self.version(BLUETOOTH, "1.1.0")
        self.base = self.index(skills=[self.skill("bluetooth", [self.v1, self.v2])])
        self.accept(self.base)
        self.file = self.reg.install(self.reg.check("io.github.ada.bluetooth", "1.0.0"))
        self.builtin = self.root / "builtin"
        self.builtin.mkdir()
        shutil.copy(BUILTIN / "time.yaml", self.builtin / "time.yaml")
        self.host = SkillHost(DEFAULTS, self.builtin, self.root / "bundled", self.skills,
                              launcher=lambda *_: None)

    def loaded(self) -> set:
        self.host.reload()
        return {skill["id"] for skill in self.host.current().listing()}

    def suspend(self, scope, kind="suspend", seq=1, reason="It went bad."):
        self.revoked(seq, [self.entry(seq, scope, kind, reason)])
        return self.reg.refresh()

    def test_install_writes_the_checked_bytes_and_a_record(self):
        self.assertEqual(self.file, self.skills / "io.github.ada.bluetooth.yaml")
        self.assertEqual(self.file.read_bytes(), BLUETOOTH.format(version="1.0.0").encode())
        record = registry.installed(self.skills)[self.file.name]
        self.assertEqual(record, {"id": "io.github.ada.bluetooth", "version": "1.0.0",
                                  "key": fingerprint(self.ada[1]), "sha256": self.v1["sha256"]})
        self.assertIn("io.github.ada.bluetooth", self.loaded())

    def test_a_suspension_switches_it_off_with_the_reason_and_a_lift_brings_it_back(self):
        self.assertIn("io.github.ada.bluetooth", self.loaded())
        changes = self.suspend({"skill": "io.github.ada.bluetooth"}, reason="Its host was lost.")
        self.assertEqual(changes, [{"file": self.file.name, "id": "io.github.ada.bluetooth",
                                    "off": True, "kind": "suspend",
                                    "reason": "Its host was lost."}])
        self.assertEqual(self.changed, 1)
        self.assertTrue(self.file.exists())
        self.assertNotIn("io.github.ada.bluetooth", self.loaded())
        self.assertEqual(self.host.problems[str(self.file)],
                         "The skills registry has switched it off: Its host was lost.")
        self.assertIn(str(self.file), self.host.offered, "it keeps its place among the six")
        self.accept(self.index(seq=2, skills=self.base["skills"], lifted=[self.lift(1)]))
        changes = self.reg.apply()
        self.assertEqual([change["off"] for change in changes], [False])
        self.assertIn("io.github.ada.bluetooth", self.loaded())
        self.assertEqual(self.host.problems, {})

    def test_the_last_version_that_worked_isnt_used_for_a_switched_off_recipe(self):
        self.assertIn("io.github.ada.bluetooth", self.loaded())
        self.suspend({"skill": "io.github.ada.bluetooth"})
        self.assertNotIn("io.github.ada.bluetooth", self.loaded())
        self.assertNotIn("still in use", self.host.problems[str(self.file)])

    def test_a_recipe_that_breaks_as_it_is_switched_off_isnt_kept_running_either(self):
        """A review half-saved the file of a recipe in use, then revoked it: the last version
        that worked was kept, so it ran on, switched off in Browse and the notification."""
        self.assertIn("io.github.ada.bluetooth", self.loaded())
        self.file.write_text(self.file.read_text() + "  broken: [\n")
        self.assertIn("io.github.ada.bluetooth", self.loaded(), "a broken file keeps it")
        self.suspend({"skill": "io.github.ada.bluetooth"}, reason="Its host was lost.")
        self.assertNotIn("io.github.ada.bluetooth", self.loaded())
        self.assertEqual(self.host.problems[str(self.file)],
                         "The skills registry has switched it off: Its host was lost.")
        self.assertNotIn("bluetooth.open", self.host.current().actions)

    def test_a_recipe_you_saved_over_one_switched_off_is_yours(self):
        self.suspend({"skill": "io.github.ada.bluetooth"})
        self.assertNotIn("io.github.ada.bluetooth", self.loaded())
        self.file.write_text(TIDES.format(version="1.0.0").replace("ada.tides", "me.tides"))
        self.assertIn("io.github.me.tides", self.loaded())
        self.assertNotIn(str(self.file), self.host.problems)

    def test_a_crash_in_the_middle_of_an_update_leaves_a_record_of_what_is_on_disk(self):
        """A review stopped an update between its two writes: the record named the new version
        over the old file, so a revocation of the old one didn't reach it."""
        def dies_at(target, nth=1):
            real, seen = registry._write, []

            def write(file, data, mode=0o600):
                if file == target:
                    seen.append(file)
                    if len(seen) == nth:
                        raise SystemExit("the daemon died here")
                real(file, data, mode)
            return mock.patch.object(registry, "_write", write)

        def record():
            return registry.installed(self.skills)[self.file.name]

        update = self.reg.check("io.github.ada.bluetooth", "1.1.0")
        with dies_at(self.file), self.assertRaises(SystemExit):
            self.reg.install(update)
        self.assertIn('"1.0.0"', self.file.read_text())
        self.assertEqual((record()["version"], record()["sha256"]), ("1.0.0", self.v1["sha256"]))
        self.assertFalse(self.reg.edited(self.file.name, record()))
        self.suspend({"skill": "io.github.ada.bluetooth", "versions": ["1.0.0"]})
        self.assertNotIn("io.github.ada.bluetooth", self.loaded())
        with dies_at(self.file), self.assertRaises(SystemExit):
            self.reg.install(update)
        self.assertIn("off", record(), "a switch-off isn't lost either")
        self.assertNotIn("io.github.ada.bluetooth", self.loaded())
        with dies_at(self.skills.parent / registry.INSTALLED, nth=2):    # after the file
            with self.assertRaises(SystemExit):
                self.reg.install(update)
        self.assertIn('"1.1.0"', self.file.read_text())
        self.assertEqual((record()["version"], record()["sha256"]), ("1.1.0", update.sha256))
        self.assertNotIn("off", record(), "the update its revocation doesn't cover ended it")
        self.assertIn("io.github.ada.bluetooth", self.loaded())

    def full(self, target: Path):
        """registry._write failing at target as on a full disk."""
        real = registry._write

        def write(file, data, mode=0o600):
            if file == target:
                raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))
            real(file, data, mode)
        return mock.patch.object(registry, "_write", write)

    def written(self) -> dict:
        """The record of the file as it is on disk, with nothing held in memory."""
        return json.loads((self.skills.parent / registry.INSTALLED).read_text())[self.file.name]

    def test_a_switch_off_that_cant_be_written_holds_while_tux_runs_and_says_so(self):
        """A review filled the disk at a revocation: the recipe ran on with nothing said, and
        the check that would write it came a day later."""
        ledger = self.skills.parent / registry.INSTALLED
        self.assertIn("io.github.ada.bluetooth", self.loaded())
        with self.full(ledger):
            changes = self.suspend({"skill": "io.github.ada.bluetooth"})
            self.assertEqual([change["off"] for change in changes], [True])
            self.assertNotIn("io.github.ada.bluetooth", self.loaded())
            self.assertIn(self.file.name, registry.switched_off(self.skills))
        self.assertEqual(self.reg.problems["state"], registry.NOT_RECORDED.format(
            file=registry._shown(ledger), why=os.strerror(errno.ENOSPC)))
        self.assertEqual(self.reg.unsaved, {"state"})
        self.assertNotIn("off", self.written(), "only in memory")
        self.assertEqual(self.reg.refresh(), [], "told once, and written now")
        self.assertIn("off", self.written())
        self.assertNotIn("state", self.reg.problems)
        self.assertEqual(self.reg.unsaved, set())
        self.assertNotIn("io.github.ada.bluetooth", self.loaded())

    def test_a_lift_that_cant_be_written_waits_for_the_write(self):
        ledger = self.skills.parent / registry.INSTALLED
        self.suspend({"skill": "io.github.ada.bluetooth"})
        self.accept(self.index(seq=2, skills=self.base["skills"], lifted=[self.lift(1)]))
        with self.full(ledger):
            self.assertEqual(self.reg.apply(), [])
            self.assertNotIn("io.github.ada.bluetooth", self.loaded())
        self.assertIn("off", self.written())
        self.assertEqual([change["off"] for change in self.reg.apply()], [False])
        self.assertIn("io.github.ada.bluetooth", self.loaded())
        with self.full(ledger):     # a switch-off held in memory only waits for it too
            self.assertEqual([change["off"] for change in self.suspend(
                {"skill": "io.github.ada.bluetooth"}, seq=2)], [True])
            self.accept(self.index(seq=3, skills=self.base["skills"], lifted=[1, 2]))
            self.assertEqual(self.reg.apply(), [])
            self.assertNotIn("io.github.ada.bluetooth", self.loaded())
        self.assertEqual([change["off"] for change in self.reg.apply()], [False])
        self.assertIn("io.github.ada.bluetooth", self.loaded())
        self.assertNotIn("off", self.written())
        self.assertNotIn("state", self.reg.problems)

    def test_a_version_the_index_no_longer_lists_isnt_installed(self):
        checked = self.reg.check("io.github.ada.bluetooth", "1.1.0")
        self.accept(self.index(seq=2, skills=[self.skill("bluetooth", [self.v1])]))
        self.refused(self.reg.install, checked, contains=registry.DROPPED)
        republished = self.version(BLUETOOTH.replace("Opens", "Shows"), "1.1.0")
        self.accept(self.index(seq=3, skills=[self.skill("bluetooth", [self.v1, republished])]))
        self.refused(self.reg.install, checked, contains=registry.DROPPED)
        self.assertIn('"1.0.0"', self.file.read_text())

    def test_remove_keeps_a_switch_off_only_a_lift_can_end(self):
        self.suspend({"skill": "io.github.ada.bluetooth"}, reason="Gone bad.")
        (self.state / "state.json").unlink()
        self.reg = self.new_registry()
        self.accept(self.index(seq=2, skills=self.base["skills"]))
        self.reg.remove("io.github.ada.bluetooth")
        self.assertFalse(self.file.exists())
        self.assertIn("off", registry.installed(self.skills)[self.file.name])
        self.assertEqual(self.reg.held_off("io.github.ada.bluetooth")["reason"], "Gone bad.")
        self.refused(self.reg.install, self.reg.check("io.github.ada.bluetooth", "1.1.0"),
                     contains="Gone bad.")
        self.accept(self.index(seq=3, skills=self.base["skills"], lifted=[self.lift(1)]))
        self.assertEqual(self.reg.apply(), [], "nothing is there to switch back on")
        self.assertIsNone(self.reg.held_off("io.github.ada.bluetooth"))
        self.reg.install(self.reg.check("io.github.ada.bluetooth", "1.1.0"))
        self.assertIn("io.github.ada.bluetooth", self.loaded())

    def test_remove_drops_the_record_of_a_switch_off_tux_can_still_follow(self):
        self.suspend({"skill": "io.github.ada.bluetooth", "versions": ["1.0.0"]})
        self.reg.remove("io.github.ada.bluetooth")
        self.assertEqual(registry.installed(self.skills), {})
        self.refused(self.reg.check, "io.github.ada.bluetooth", "1.0.0", contains="It went bad.")

    def test_a_switched_off_recipe_never_holds_back_another_taken_out(self):
        """Its id is known though it doesn't load, so it isn't taken for a skill renamed onto a
        broken file, and a recipe of yours taken out in the same reload counts as taken out."""
        self.file.unlink()
        mine = self.skills / "tides.yaml"
        mine.write_text(TIDES.format(version="1.0.0").replace("ada.tides", "me.tides"))
        self.assertIn("io.github.me.tides", self.loaded())
        self.reg.install(self.reg.check("io.github.ada.bluetooth", "1.0.0"))
        self.suspend({"skill": "io.github.ada.bluetooth"})
        mine.unlink()
        self.assertNotIn("io.github.ada.bluetooth", self.loaded())
        self.assertTrue(self.host.grants.records["io.github.me.tides"].get("removed"))

    def test_a_ban_says_it_can_be_removed(self):
        self.suspend({"author": "ada"}, kind="ban")
        self.loaded()
        self.assertIn("for good, and you can remove it", self.host.problems[str(self.file)])

    def test_by_key_and_by_author(self):
        for seq, scope in ((1, {"key": fingerprint(self.ada[1])}), (2, {"author": "ada"})):
            with self.subTest(scope):
                self.suspend(scope, seq=seq)
                self.assertNotIn("io.github.ada.bluetooth", self.loaded())

    def test_other_versions_and_skills_stay_on(self):
        self.assertEqual(self.suspend({"skill": "io.github.ada.bluetooth",
                                       "versions": ["1.1.0"]}), [])
        self.assertEqual(self.suspend({"key": fingerprint(self.eve[1])}, seq=2), [])
        self.assertEqual(self.suspend({"author": "adam"}, seq=3), [])
        self.assertIn("io.github.ada.bluetooth", self.loaded())

    def test_a_recipe_you_wrote_or_copied_in_is_left_alone(self):
        mine = self.skills / "bluetooth.yaml"
        self.file.rename(mine)
        self.suspend({"skill": "io.github.ada.bluetooth"})
        self.assertIn("io.github.ada.bluetooth", self.loaded())

    def test_a_switch_off_outlasts_a_lost_state_until_it_is_lifted(self):
        self.suspend({"skill": "io.github.ada.bluetooth"})
        (self.state / "state.json").unlink()
        self.reg = self.new_registry()
        self.assertEqual(self.reg.apply(), [])
        self.assertNotIn("io.github.ada.bluetooth", self.loaded())

    def test_a_revocation_that_lands_while_the_card_is_open_stops_the_install(self):
        self.file.unlink()
        registry._write(self.skills.parent / registry.INSTALLED, b"{}")
        checked = self.reg.check("io.github.ada.bluetooth", "1.1.0")
        self.revoked(1, [self.entry(1, {"author": "ada"}, reason="Gone bad.")])
        self.reg.accept_revocations(self.files["revocations.json"],
                                    self.files["revocations.json.sig"])
        self.refused(self.reg.install, checked, contains="switched it off: Gone bad.")
        self.assertFalse(self.file.exists())
        self.assertEqual(registry.installed(self.skills), {})

    def test_an_update_keeps_a_switch_off_the_state_has_forgotten(self):
        self.suspend({"author": "ada"}, reason="Gone bad.")
        (self.state / "state.json").unlink()
        self.reg = self.new_registry()
        self.accept(self.index(seq=2, skills=self.base["skills"]))
        checked = self.reg.check("io.github.ada.bluetooth", "1.1.0")
        self.refused(self.reg.install, checked, contains="switched it off: Gone bad.")
        self.assertEqual(registry.installed(self.skills)[self.file.name]["version"], "1.0.0")
        self.assertIn('"1.0.0"', self.file.read_text())
        self.assertIn(self.file.name, registry.switched_off(self.skills))

    def test_a_narrower_copy_of_the_same_id_doesnt_end_a_switch_off_tux_lost(self):
        self.revoked(1, [self.entry(7, {"author": "ada"}, reason="Gone bad.")])
        self.reg.refresh()
        before = registry.switched_off(self.skills)
        self.assertIn(self.file.name, before)
        shutil.rmtree(self.state)
        self.reg = self.new_registry()
        self.accept(self.index(seq=2, skills=self.base["skills"]))
        self.revoked(1, [self.entry(7, {"skill": "io.github.ada.bluetooth",
                                        "versions": ["1.0.0"]}, reason="Narrow.")])
        self.reg.refresh()
        self.assertEqual(registry.switched_off(self.skills), before)
        self.refused(self.reg.install, self.reg.check("io.github.ada.bluetooth", "1.1.0"),
                     contains="Gone bad.")
        self.assertEqual(registry.installed(self.skills)[self.file.name]["version"], "1.0.0")
        self.assertEqual(registry.switched_off(self.skills), before)

    def test_an_update_its_revocation_doesnt_cover_ends_the_switch_off(self):
        self.suspend({"skill": "io.github.ada.bluetooth", "versions": ["1.0.0"]})
        self.assertNotIn("io.github.ada.bluetooth", self.loaded())
        self.reg.install(self.reg.check("io.github.ada.bluetooth", "1.1.0"))
        self.assertEqual(registry.switched_off(self.skills), {})
        self.assertIn("io.github.ada.bluetooth", self.loaded())
        self.assertEqual(self.reg.apply(), [])

    def test_a_ban_found_by_the_registry_switches_off_after_a_moderation_copy(self):
        self.revoked(1, [self.entry(2, {"skill": "io.github.ada.nothing", "versions": ["9"]})])
        self.reg.refresh()
        authors = {"ada": {**self.base["authors"]["ada"], "status": "banned"}}
        self.accept(self.index(seq=2, skills=self.base["skills"], authors=authors,
                               revocations=[self.entry(2, {"author": "ada"}, "ban")]))
        changes = self.reg.apply()
        self.assertEqual([(c["off"], c["kind"]) for c in changes], [(True, "ban")])
        self.assertIn(self.file.name, registry.switched_off(self.skills))
        self.refused(self.reg.check, "io.github.ada.bluetooth", "1.0.0", contains="for good")

    def test_install_leaves_your_own_file_and_your_edits_alone(self):
        self.file.write_text(self.file.read_text().replace("Bluetooth page", "My Bluetooth"))
        self.refused(self.reg.install, self.reg.check("io.github.ada.bluetooth", "1.1.0"),
                     contains="You've changed")
        self.file.unlink()
        self.reg.install(self.reg.check("io.github.ada.bluetooth", "1.1.0"))
        self.assertIn('"1.1.0"', self.file.read_text())
        registry._write(self.skills.parent / registry.INSTALLED, b"{}")
        self.refused(self.reg.install, self.reg.check("io.github.ada.bluetooth", "1.1.0"),
                     contains="already have a skill file")


if __name__ == "__main__":
    unittest.main()
