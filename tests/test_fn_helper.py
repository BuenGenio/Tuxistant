"""Fn helper tests: synthetic input_event bytes through a FIFO, never a real keyboard.

The privacy tests are the point of this file. Silence is proved with a sentinel Fn tap: the
stream is ordered, so anything leaked by the keys before it would arrive before its `down`.
"""
import ast
import importlib.machinery
import importlib.util
import os
import random
import shutil
import signal
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HELPER = ROOT / "helper" / "tuxistant-fn"
KEY_FN = 0x1D0
EV_SYN, EV_KEY, EV_MSC = 0, 1, 4
SYN_DROPPED = 3
KEY_A, KEY_Z, KEY_ENTER, KEY_LEFTSHIFT = 30, 44, 28, 42
ALL_KEYS = [code for code in range(1, 0x300) if code != KEY_FN]
# A laptop's built-in keyboard and its touchpad.
REAL_KEYBOARD_CAPS = ("10000 0 0 0 101007b02011007 ff9f217ac14057ff "
                      "ffbeffdfffefffff fffffffffffffffe")
REAL_TOUCHPAD_CAPS = "e520 10000 0 0 0 0"


def load_helper():
    """The helper has no .py suffix and must not leave a __pycache__ next to itself."""
    loader = importlib.machinery.SourceFileLoader("tuxistant_fn", str(HELPER))
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
    previous, sys.dont_write_bytecode = sys.dont_write_bytecode, True
    try:
        loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous
    return module


fn = load_helper()


def event(kind, code, value):
    return struct.pack("@llHHi", 1700000000, 123456, kind, code, value)


def key(code, value):
    # A real keyboard reports the scan code as EV_MSC next to each key event.
    return event(EV_MSC, 4, code) + event(EV_KEY, code, value) + event(EV_SYN, 0, 0)


def tap(code):
    return key(code, 1) + key(code, 0)


FN_DOWN, FN_UP = key(KEY_FN, 1), key(KEY_FN, 0)


def start_helper(event, sock, **kwargs):
    # stdin too: the tests count sockets in the helper's fd table, and it could inherit one.
    return subprocess.Popen([sys.executable, str(HELPER), "--event", event, "--socket", sock,
                             "--rescan", "0.02"], stdin=subprocess.DEVNULL, **kwargs)


def wait_for(condition, what, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = condition()
        if result:
            return result
        time.sleep(0.005)
    raise AssertionError(f"timed out waiting for {what}")


class Client:
    def __init__(self, path):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(path)
        self.sock.settimeout(5)
        self.buffer = b""

    def lines(self, count):
        while self.buffer.count(b"\n") < count:
            data = self.sock.recv(65536)
            if not data:
                raise AssertionError(f"socket closed after {self.buffer!r}")
            self.buffer += data
        parts = self.buffer.split(b"\n")
        self.buffer = b"\n".join(parts[count:])
        return [part.decode() for part in parts[:count]]

    def close(self):
        self.sock.close()


class Running(unittest.TestCase):
    """Runs the real script as a process, as the unit would."""

    def setUp(self):
        # Not TMPDIR: AF_UNIX paths are limited to 107 bytes.
        self.tmp = tempfile.mkdtemp(prefix="tfn", dir="/tmp")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.fifo = f"{self.tmp}/event"
        self.sock = f"{self.tmp}/events.sock"
        os.mkfifo(self.fifo)
        self.log = open(f"{self.tmp}/log", "wb")
        self.addCleanup(self.log.close)
        self.proc = start_helper(self.fifo, self.sock, stdout=self.log, stderr=self.log)
        self.addCleanup(self.stop)
        self.writer = None
        self.open_writer()
        wait_for(lambda: os.path.exists(self.sock), "the socket")

    def stop(self):
        if self.writer is not None:
            os.close(self.writer)
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.wait()

    def open_writer(self):
        def attempt():
            try:
                return os.open(self.fifo, os.O_WRONLY | os.O_NONBLOCK) + 1
            except OSError:  # ENXIO until the helper has the FIFO open
                return 0
        self.writer = wait_for(attempt, "the helper to open the FIFO") - 1
        os.set_blocking(self.writer, True)

    def close_writer(self):
        os.close(self.writer)
        self.writer = None

    def send(self, data):
        os.write(self.writer, data)

    def sockets_open(self, pid=None):
        pid = pid or self.proc.pid
        count = 0
        for fd in os.listdir(f"/proc/{pid}/fd"):
            try:
                count += os.readlink(f"/proc/{pid}/fd/{fd}").startswith("socket:")
            except OSError:
                pass
        return count

    def wait_clients(self, count):
        wait_for(lambda: self.sockets_open() == count + 1, f"{count} clients")

    def client(self):
        """Returns once the helper has accepted, so nothing sent afterwards can be missed."""
        before = self.sockets_open() - 1
        client = Client(self.sock)
        self.addCleanup(client.close)
        self.wait_clients(before + 1)
        return client

    def output(self):
        self.log.flush()
        return Path(f"{self.tmp}/log").read_bytes()


class Privacy(Running):
    def test_typing_with_fn_up_produces_no_output_at_all(self):
        client = self.client()
        for code in ALL_KEYS:
            self.send(key(code, 1) + key(code, 2) + key(code, 2) + key(code, 0))
        self.send(FN_DOWN + FN_UP)
        self.assertEqual(client.lines(2), ["down", "up"])
        self.assertEqual(client.buffer, b"")
        self.assertEqual(self.output(), b"", "the helper logged something during typing")

    def test_typing_after_a_hold_is_silent_again(self):
        client = self.client()
        self.send(FN_DOWN + tap(KEY_A) + FN_UP + tap(KEY_A) + tap(KEY_Z))
        self.send(FN_DOWN + FN_UP)
        self.assertEqual(client.lines(5), ["down", "other", "up", "down", "up"])
        self.assertEqual(client.buffer, b"")

    def test_output_during_a_hold_never_depends_on_the_key(self):
        client = self.client()
        streams = set()
        for code in ALL_KEYS:
            self.send(FN_DOWN + tap(code) + FN_UP)
            streams.add(tuple(client.lines(3)))
        self.assertEqual(streams, {("down", "other", "up")})
        self.assertEqual(self.output(), b"")

    def test_key_identity_cannot_be_recovered_from_any_stream(self):
        """Random typing, then the same typing with every key swapped: identical bytes."""
        client = self.client()
        for seed in range(25):
            rng = random.Random(seed)
            script = [(rng.choice([KEY_FN] + ALL_KEYS[:80]), rng.choice([0, 1, 1, 2]))
                      for _ in range(300)]
            swapped = dict(zip(ALL_KEYS, rng.sample(ALL_KEYS, len(ALL_KEYS))))
            swapped[KEY_FN] = KEY_FN
            expected = oracle(script)
            for variant in (script, [(swapped[code], value) for code, value in script]):
                self.send(b"".join(key(code, value) for code, value in variant) + FN_UP)
                self.send(FN_DOWN + FN_UP)  # sentinel, also leaves Fn released for the next run
                self.assertEqual(client.lines(len(expected) + 2), expected + ["down", "up"], seed)

    def test_only_the_three_words_can_ever_be_produced(self):
        state = fn.FnState()
        seen = set()
        rng = random.Random(1)
        for _ in range(200000):
            seen.add(state.key(rng.choice("ab"), rng.choice([KEY_FN, KEY_A, rng.randrange(0x300)]),
                               rng.choice([0, 1, 2, -1, 3])))
            if rng.random() < 0.01:
                seen.add(state.release(rng.choice("ab")))
        self.assertEqual(seen, {None, "down", "up", "other"})

    def test_source_has_no_way_out_but_the_socket(self):
        tree = ast.parse(HELPER.read_text())
        imports = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports |= {alias.name for alias in node.names}
            elif isinstance(node, ast.ImportFrom):
                imports.add(node.module)
        self.assertLessEqual(imports, {"argparse", "os", "selectors", "signal", "socket", "struct",
                                       "sys", "time"})
        source = HELPER.read_text()
        for word in ("EVIOCGRAB", "AF_INET", "subprocess", "code}", "value}"):
            self.assertNotIn(word, source)


def oracle(script):
    """Independent statement of the contract, for one device."""
    words, held, other = [], False, False
    for code, value in script:
        if code == KEY_FN:
            if value == 1 and not held:
                held, other = True, False
                words.append("down")
            elif value == 0 and held:
                held = False
                words.append("up")
        elif value == 1 and held and not other:
            other = True
            words.append("other")
    if held:
        words.append("up")  # the FN_UP the test appends
    return words


class Protocol(Running):
    def test_fn_tap(self):
        client = self.client()
        self.send(FN_DOWN + FN_UP)
        self.assertEqual(client.lines(2), ["down", "up"])

    def test_other_once_per_hold_and_again_on_the_next(self):
        client = self.client()
        self.send(FN_DOWN + tap(KEY_A) + tap(KEY_Z) + tap(KEY_ENTER) + FN_UP)
        self.send(FN_DOWN + tap(KEY_A) + FN_UP)
        self.assertEqual(client.lines(6), ["down", "other", "up", "down", "other", "up"])

    def test_repeats_and_releases_are_not_presses(self):
        client = self.client()
        self.send(key(KEY_A, 1) + FN_DOWN + key(KEY_A, 2) + key(KEY_A, 2) + key(KEY_A, 0) + FN_UP)
        self.send(FN_DOWN + key(KEY_FN, 2) + key(KEY_FN, 2) + FN_UP)
        self.assertEqual(client.lines(4), ["down", "up", "down", "up"])

    def test_a_key_held_through_the_press_only_counts_when_pressed_again(self):
        client = self.client()
        self.send(key(KEY_LEFTSHIFT, 1) + FN_DOWN + key(KEY_LEFTSHIFT, 0) + key(KEY_LEFTSHIFT, 1) + FN_UP)
        self.assertEqual(client.lines(3), ["down", "other", "up"])

    def test_duplicate_fn_press_and_stray_release(self):
        client = self.client()
        self.send(FN_UP + FN_DOWN + FN_DOWN + FN_UP + FN_UP)
        self.assertEqual(client.lines(2), ["down", "up"])
        self.send(FN_DOWN + FN_UP)
        self.assertEqual(client.lines(2), ["down", "up"])

    def test_other_event_types_are_ignored(self):
        client = self.client()
        noise = (event(EV_MSC, 4, 1) + event(EV_MSC, KEY_FN, 1) + event(2, KEY_A, 1)
                 + event(3, KEY_FN, 1) + event(17, 0, 1))
        self.send(noise + FN_DOWN + noise + FN_UP)
        self.assertEqual(client.lines(2), ["down", "up"])

    def test_events_split_across_reads(self):
        client = self.client()
        data = FN_DOWN + tap(KEY_A) + FN_UP
        for start in range(0, len(data), 7):
            self.send(data[start:start + 7])
            time.sleep(0.001)
        self.assertEqual(client.lines(3), ["down", "other", "up"])

    def test_socket_is_a_world_connectable_socket(self):
        mode = os.stat(self.sock).st_mode
        self.assertTrue(stat.S_ISSOCK(mode))
        self.assertEqual(stat.S_IMODE(mode), 0o666)

    def test_broadcast_to_every_client(self):
        clients = [self.client() for _ in range(3)]
        self.send(FN_DOWN + tap(KEY_A) + FN_UP)
        for client in clients:
            self.assertEqual(client.lines(3), ["down", "other", "up"])

    def test_dead_client_is_forgotten(self):
        gone, kept = self.client(), self.client()
        gone.close()
        self.wait_clients(1)
        self.send(FN_DOWN + FN_UP)
        self.assertEqual(kept.lines(2), ["down", "up"])

    def test_client_that_talks_is_dropped_and_changes_nothing(self):
        rude, kept = self.client(), self.client()
        rude.sock.send(b"which key was that?\n")
        self.wait_clients(1)
        self.send(FN_DOWN + FN_UP)
        self.assertEqual(kept.lines(2), ["down", "up"])

    def test_client_that_never_reads_does_not_stall_the_others(self):
        stuck, reader = self.client(), self.client()
        for _ in range(100):
            self.send((FN_DOWN + tap(KEY_A) + FN_UP) * 50)
            self.assertEqual(reader.lines(150), ["down", "other", "up"] * 50)
        self.wait_clients(1)  # the full buffer got the stuck one dropped

    def test_client_limit_refuses_without_harm(self):
        clients = [self.client() for _ in range(fn.MAX_CLIENTS)]
        extra = Client(self.sock)
        self.addCleanup(extra.close)
        self.assertEqual(extra.sock.recv(16), b"")
        self.send(FN_DOWN + FN_UP)
        self.assertEqual(clients[-1].lines(2), ["down", "up"])

    def test_device_loss_during_a_hold_ends_it_and_the_device_comes_back(self):
        client = self.client()
        self.send(FN_DOWN)
        self.assertEqual(client.lines(1), ["down"])
        self.close_writer()
        self.assertEqual(client.lines(1), ["up"])
        self.open_writer()
        self.send(FN_DOWN + tap(KEY_A) + FN_UP)
        self.assertEqual(client.lines(3), ["down", "other", "up"])

    def test_missing_device_appears_later(self):
        late = f"{self.tmp}/late"
        sock = f"{self.tmp}/late.sock"
        proc = start_helper(late, sock, stderr=subprocess.DEVNULL)
        self.addCleanup(proc.wait)
        self.addCleanup(proc.kill)
        wait_for(lambda: os.path.exists(sock), "the socket")
        client = Client(sock)
        self.addCleanup(client.close)
        wait_for(lambda: self.sockets_open(proc.pid) == 2, "the client to be accepted")
        os.mkfifo(late)

        def attempt():
            try:
                return os.open(late, os.O_WRONLY | os.O_NONBLOCK) + 1
            except OSError:
                return 0
        writer = wait_for(attempt, "the helper to find the device") - 1
        self.addCleanup(os.close, writer)
        os.write(writer, FN_DOWN + FN_UP)
        self.assertEqual(client.lines(2), ["down", "up"])

    def test_events_lost_in_the_kernel_end_the_hold(self):
        client = self.client()
        self.send(FN_DOWN + event(EV_SYN, SYN_DROPPED, 0))  # the release may be what was lost
        self.assertEqual(client.lines(2), ["down", "up"])
        self.send(FN_UP + FN_DOWN + FN_UP)
        self.assertEqual(client.lines(2), ["down", "up"])

    def test_sigterm_during_a_hold_says_up_and_removes_the_socket(self):
        client = self.client()
        self.send(FN_DOWN)
        self.assertEqual(client.lines(1), ["down"])
        self.proc.send_signal(signal.SIGTERM)
        self.assertEqual(client.lines(1), ["up"])
        self.assertEqual(self.proc.wait(5), 0)
        self.assertFalse(os.path.exists(self.sock))

    def test_stale_socket_is_replaced(self):
        self.proc.kill()
        self.proc.wait()
        self.assertTrue(os.path.exists(self.sock))
        self.close_writer()
        self.proc = start_helper(self.fifo, self.sock)
        self.open_writer()
        wait_for(lambda: self.sockets_open() == 1, "the new socket")
        client = self.client()
        self.send(FN_DOWN + FN_UP)
        self.assertEqual(client.lines(2), ["down", "up"])


class InProcess(unittest.TestCase):
    """Steps the loop by hand where a real process could not be made to suspend."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="tfn", dir="/tmp")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.fifo = f"{self.tmp}/event"
        os.mkfifo(self.fifo)
        self.helper = fn.Helper(f"{self.tmp}/s", [self.fifo], 1)
        self.addCleanup(self.close_helper)
        self.helper.scan()
        self.writer = os.open(self.fifo, os.O_WRONLY | os.O_NONBLOCK)
        self.addCleanup(os.close, self.writer)
        self.client = Client(f"{self.tmp}/s")
        self.addCleanup(self.client.close)
        self.helper.accept()

    def close_helper(self):
        for conn in list(self.helper.clients):
            self.helper.drop_client(conn)
        for path in list(self.helper.devices):
            self.helper.drop(path)
        self.helper.server.close()
        self.helper.sel.close()

    def test_resume_reopens_devices_and_ends_a_hold(self):
        os.write(self.writer, FN_DOWN)
        self.helper.read(self.fifo)
        self.assertEqual(self.client.lines(1), ["down"])
        self.helper.sleep_offset -= 60  # as if BOOTTIME ran ahead of MONOTONIC by a minute
        self.helper.scan()
        self.assertEqual(self.client.lines(1), ["up"])
        self.assertIn(self.fifo, self.helper.devices)
        os.write(self.writer, FN_DOWN)
        self.helper.read(self.fifo)
        self.assertEqual(self.client.lines(1), ["down"])

    def test_no_reopen_without_a_suspend(self):
        before = self.helper.devices[self.fifo]
        os.write(self.writer, FN_DOWN)
        self.helper.read(self.fifo)
        self.helper.scan()
        self.assertEqual(self.helper.devices[self.fifo], before)
        self.assertEqual(self.helper.state.held, {self.fifo})

    def test_two_keyboards_are_one_fn(self):
        state = fn.FnState()
        self.assertEqual(state.key("a", KEY_FN, 1), "down")
        self.assertIsNone(state.key("b", KEY_FN, 1))
        self.assertEqual(state.key("b", KEY_A, 1), "other")
        self.assertIsNone(state.key("a", KEY_FN, 0))
        self.assertEqual(state.key("b", KEY_FN, 0), "up")


class Discovery(unittest.TestCase):
    def test_capability_bitmap(self):
        self.assertTrue(fn.has_key(REAL_KEYBOARD_CAPS, KEY_FN))
        self.assertTrue(fn.has_key(REAL_KEYBOARD_CAPS, KEY_A))
        self.assertFalse(fn.has_key(REAL_TOUCHPAD_CAPS, KEY_FN))
        self.assertFalse(fn.has_key("0", KEY_FN))
        self.assertFalse(fn.has_key("", KEY_FN))
        self.assertFalse(fn.has_key("10000", KEY_FN))  # right bit, wrong word

    def test_finds_only_devices_with_fn(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name, caps in (("event0", REAL_KEYBOARD_CAPS + "\n"), ("event1", REAL_TOUCHPAD_CAPS),
                               ("event2", "0\n"), ("event3", "not hex"), ("mouse0", REAL_KEYBOARD_CAPS)):
                folder = Path(tmp, name, "device/capabilities")
                folder.mkdir(parents=True)
                (folder / "key").write_text(caps)
            Path(tmp, "event4").mkdir()
            self.assertEqual(fn.fn_devices(tmp, "/dev/input"), ["/dev/input/event0"])

    def test_input_event_layout(self):
        self.assertEqual(fn.EVENT.size, struct.calcsize("@llHHi"))
        if struct.calcsize("l") == 8:
            self.assertEqual(fn.EVENT.size, 24)


class Script(unittest.TestCase):
    def test_runs_through_its_shebang(self):
        self.assertTrue(os.access(HELPER, os.X_OK))
        done = subprocess.run([str(HELPER), "--help"], capture_output=True, text=True, timeout=10)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("--event", done.stdout)


if __name__ == "__main__":
    unittest.main()
