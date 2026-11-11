"""The skill folder watcher, on real temp folders: inotify and the polling fallback."""
import asyncio
import errno
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tuxistant import inotify  # noqa: E402
from tuxistant.inotify import Watcher, ignored  # noqa: E402

DEBOUNCE = 0.05
TIMEOUT = 3.0


class Base(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="tux-inotify-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        env = {k: str(self.tmp / k.lower()) for k in
               ("XDG_DATA_HOME", "XDG_CONFIG_HOME", "XDG_STATE_HOME")}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.root = self.tmp / "skills"
        self.root.mkdir()
        self.calls = 0

    def bump(self):
        self.calls += 1

    async def watch(self, roots=None, **attrs):
        watcher = Watcher(roots or [self.root], self.bump, debounce_s=DEBOUNCE)
        for name, value in attrs.items():
            setattr(watcher, name, value)
        await watcher.start()
        self.addCleanup(watcher.stop)
        return watcher

    async def changes(self, expected=1, quiet=6 * DEBOUNCE):
        """Wait for `expected` more callbacks, then make sure no extra one follows."""
        target = self.calls + expected
        deadline = asyncio.get_running_loop().time() + TIMEOUT
        while self.calls < target and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.01)
        await asyncio.sleep(quiet)
        got = self.calls - (target - expected)
        self.assertEqual(got, expected)

    def skill(self, name="hello", root=None):
        folder = (root or self.root) / name
        folder.mkdir(parents=True)
        (folder / "skill.yaml").write_text("id: org.example.hello\n")
        (folder / "main.py").write_text("print('hi')\n")
        return folder


class Inotify(Base):
    async def test_it_really_uses_inotify(self):
        watcher = await self.watch()
        self.assertFalse(watcher.polling)

    async def test_new_skill_folder(self):
        watcher = await self.watch()
        folder = self.skill()
        await self.changes(1)
        self.assertIn(folder, self.watched(watcher))

    async def test_editing_a_file_in_a_skill(self):
        folder = self.skill()
        await self.watch()
        (folder / "main.py").write_text("print('changed')\n")
        await self.changes(1)

    async def test_adding_and_deleting_a_file_in_a_skill(self):
        folder = self.skill()
        await self.watch()
        (folder / "extra.py").write_text("x = 1\n")
        await self.changes(1)
        (folder / "extra.py").unlink()
        await self.changes(1)

    async def test_deleting_a_skill_folder(self):
        folder = self.skill()
        watcher = await self.watch()
        shutil.rmtree(folder)
        await self.changes(1)
        self.assertNotIn(folder, self.watched(watcher))

    async def test_renaming_a_skill_folder(self):
        old = self.skill()
        watcher = await self.watch()
        new = self.root / "renamed"
        old.rename(new)
        await self.changes(1)
        self.assertEqual({self.root, new}, self.watched(watcher))
        (new / "main.py").write_text("print('in the new place')\n")
        await self.changes(1)

    async def test_moving_a_skill_in_and_out(self):
        outside = self.skill(root=self.tmp / "elsewhere")
        watcher = await self.watch()
        outside.rename(self.root / "hello")
        await self.changes(1)
        (self.root / "hello").rename(self.tmp / "gone")
        await self.changes(1)
        self.assertEqual({self.root}, self.watched(watcher))
        (self.tmp / "gone" / "main.py").write_text("not ours any more\n")
        await self.changes(0)

    async def test_editor_atomic_save_is_one_change(self):
        folder = self.skill()
        await self.watch()
        temp = folder / "skill.yaml.tmp"
        temp.write_text("id: org.example.hello\nname: Hello\n")
        os.replace(temp, folder / "skill.yaml")
        await self.changes(1)

    async def test_vim_style_save_with_probe_swap_and_backup(self):
        folder = self.skill()
        await self.watch()
        (folder / "4913").write_text("")
        (folder / "4913").unlink()
        (folder / ".skill.yaml.swp").write_text("swap")
        (folder / "skill.yaml").rename(folder / "skill.yaml~")
        (folder / "skill.yaml").write_text("id: org.example.hello\n")
        (folder / "skill.yaml~").unlink()
        await self.changes(1)

    async def test_a_burst_is_one_change(self):
        folder = self.skill()
        await self.watch()
        for i in range(50):
            (folder / f"file{i % 5}.py").write_text(f"x = {i}\n")
        (self.root / "second").mkdir()
        (self.root / "second" / "skill.yaml").write_text("id: org.example.second\n")
        await self.changes(1)

    async def test_ignored_files_dont_fire(self):
        folder = self.skill()
        await self.watch()
        (folder / "__pycache__").mkdir()
        (folder / "__pycache__" / "main.cpython-314.pyc").write_bytes(b"\0")
        (folder / "main.pyc").write_bytes(b"\0")
        (folder / ".main.py.swp").write_text("swap")
        (folder / "main.py~").write_text("backup")
        (folder / ".#main.py").write_text("lock")
        (folder / "4913").write_text("")
        (self.root / "__pycache__").mkdir()
        (self.root / "__pycache__" / "x.pyc").write_bytes(b"\0")
        await self.changes(0)

    async def test_a_file_directly_in_a_root(self):
        (self.root / "weather.yaml").write_text("id: weather\n")
        await self.watch()
        (self.root / "weather.yaml").write_text("id: weather\nname: Weather\n")
        await self.changes(1)

    async def test_several_roots(self):
        other = self.tmp / "bundled"
        other.mkdir()
        await self.watch([self.root, other])
        self.skill(root=other)
        await self.changes(1)
        self.skill(root=self.root)
        await self.changes(1)

    async def test_a_missing_root_that_appears_later(self):
        root = self.tmp / "data" / "tuxistant" / "skills"
        watcher = await self.watch([root])
        await self.changes(0)
        root.mkdir(parents=True)
        await self.changes(1)
        self.assertIn(root, self.watched(watcher))
        folder = self.skill(root=root)
        await self.changes(1)
        (folder / "main.py").write_text("print('again')\n")
        await self.changes(1)

    async def test_a_root_that_appears_with_skills_already_inside(self):
        staging = self.tmp / "staging"
        self.skill(root=staging)
        root = self.tmp / "later"
        watcher = await self.watch([root])
        staging.rename(root)
        await self.changes(1)
        self.assertIn(root / "hello", self.watched(watcher))

    async def test_a_root_that_goes_away_and_comes_back(self):
        self.skill()
        watcher = await self.watch()
        shutil.rmtree(self.root)
        await self.changes(1)
        self.assertNotIn(self.root, self.watched(watcher))
        self.root.mkdir()
        await self.changes(1)
        self.skill()
        await self.changes(1)

    async def test_a_renamed_root_is_no_longer_followed(self):
        watcher = await self.watch()
        self.root.rename(self.tmp / "moved")
        await self.changes(1)
        self.assertNotIn(self.root, self.watched(watcher))
        (self.tmp / "moved" / "late.yaml").write_text("x\n")
        await self.changes(0)

    async def test_start_never_raises_for_missing_roots(self):
        await self.watch([self.tmp / "no" / "such" / "place", Path("/nonexistent-tux/skills")])
        await self.changes(0)

    async def test_stop_releases_the_descriptor_and_goes_quiet(self):
        watcher = await self.watch()
        fd = watcher._inotify.fd
        os.fstat(fd)
        watcher.stop()
        with self.assertRaises(OSError):
            os.fstat(fd)
        self.skill()
        await self.changes(0)
        watcher.stop()      # twice is fine

    async def test_stop_cancels_a_pending_change(self):
        watcher = await self.watch()
        self.skill()
        await asyncio.sleep(0.01)
        watcher.stop()
        await self.changes(0)

    async def test_a_failing_callback_keeps_the_watcher_alive(self):
        watcher = Watcher([self.root], self.explode, debounce_s=DEBOUNCE)
        await watcher.start()
        self.addCleanup(watcher.stop)
        with self.assertLogs("tuxistant", "ERROR"):
            self.skill("one")
            await self.changes(1)
        with self.assertLogs("tuxistant", "ERROR"):
            self.skill("two")
            await self.changes(1)

    async def test_an_async_callback_is_run(self):
        done = asyncio.Event()

        async def reload():
            done.set()
        watcher = Watcher([self.root], reload, debounce_s=DEBOUNCE)
        await watcher.start()
        self.addCleanup(watcher.stop)
        self.skill()
        await asyncio.wait_for(done.wait(), TIMEOUT)

    async def test_running_out_of_watches_falls_back_to_polling(self):
        def full(_self, _path):
            raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))
        with mock.patch.object(inotify._Inotify, "add", full), \
                self.assertLogs("tuxistant", "WARNING"):
            watcher = await self.watch(poll_s=0.05)
        self.assertTrue(watcher.polling)
        self.assertIsNone(watcher._inotify)
        self.skill()
        await self.changes(1)

    def explode(self):
        self.calls += 1
        raise RuntimeError("boom")

    @staticmethod
    def watched(watcher):
        return {path for targets in watcher._watches.values() for path, kind in targets
                if kind != "anchor"}


class Polling(Base):
    async def watch(self, roots=None, **attrs):
        with mock.patch.object(inotify, "_inotify_init", return_value=None):
            return await super().watch(roots, poll_s=0.05, **attrs)

    async def test_it_polls(self):
        watcher = await self.watch()
        self.assertTrue(watcher.polling)

    async def test_create_modify_rename_delete(self):
        await self.watch()
        folder = self.skill()
        await self.changes(1)
        (folder / "main.py").write_text("print('a longer line than before')\n")
        await self.changes(1)
        folder.rename(self.root / "renamed")
        await self.changes(1)
        shutil.rmtree(self.root / "renamed")
        await self.changes(1)

    async def test_ignored_files_dont_fire(self):
        folder = self.skill()
        await self.watch()
        (folder / "__pycache__").mkdir()
        (folder / "main.pyc").write_bytes(b"\0")
        (folder / ".main.py.swp").write_text("swap")
        (folder / "4913").write_text("")
        await self.changes(0)

    async def test_a_missing_root_that_appears_later(self):
        root = self.tmp / "data" / "skills"
        await self.watch([root])
        root.mkdir(parents=True)
        await self.changes(1)
        self.skill(root=root)
        await self.changes(1)

    async def test_stop_ends_polling(self):
        watcher = await self.watch()
        watcher.stop()
        self.skill()
        await self.changes(0)


class Names(unittest.TestCase):
    def test_ignored(self):
        for name in ("__pycache__", "x.pyc", ".skill.yaml.swp", "main.py~", ".#main.py", "4913"):
            self.assertTrue(ignored(name), name)
        for name in ("skill.yaml", "main.py", "skill.yaml.tmp", "lib", ".hidden", "49130"):
            self.assertFalse(ignored(name), name)


if __name__ == "__main__":
    unittest.main()
