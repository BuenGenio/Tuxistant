"""The real Mic and Speaker through real pw-record and pw-cat, in a private PipeWire graph.

The graph holds one null sink and nothing else: no ALSA, no session manager, no way to reach a
real device. Its socket lives in a temporary runtime folder, and the test stops unless pw-cli
confirms that is what it reached. A tone goes from the Speaker into the sink, and the Mic records
the sink's monitor, which proves the raw 24 kHz mono s16 format end to end.
"""
import array
import asyncio
import math
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tuxistant.audio import BYTES_PER_S, RATE, Mic, Speaker, level  # noqa: E402

GRAPH = "tuxistant-test-graph"
PORTS = "adapter.auto-port-config = { mode = dsp monitor = %s control = false position = preserve }"
CONFIG = """
context.properties = { core.daemon = true core.name = %(graph)s support.dbus = false
                       default.clock.rate = 48000 mem.mlock-all = false }
context.spa-libs = { audio.convert.* = audioconvert/libspa-audioconvert
                     support.* = support/libspa-support }
context.modules = [
    { name = libpipewire-module-protocol-native }
    { name = libpipewire-module-client-node }
    { name = libpipewire-module-adapter }
    { name = libpipewire-module-spa-node-factory }
    { name = libpipewire-module-link-factory }
    { name = libpipewire-module-metadata }
    { name = libpipewire-module-access
      args = { access.socket = { %(graph)s = "unrestricted" %(graph)s-manager = "unrestricted" } } }
]
context.objects = [
    { factory = spa-node-factory
      args = { factory.name = support.node.driver node.name = Dummy-Driver
               priority.driver = 20000 node.group = tux } }
    { factory = adapter
      args = { factory.name = support.null-audio-sink node.name = tux-loop
               media.class = Audio/Sink audio.position = [ MONO ] node.group = tux
               monitor.channel-volumes = false %(ports)s } }
]
""" % {"graph": GRAPH, "ports": PORTS % "true"}
TONE_HZ = 440
TONE_S = 2.0


def tone(seconds: float) -> bytes:
    return array.array("h", (int(0.3 * 32767 * math.sin(2 * math.pi * TONE_HZ * i / RATE))
                             for i in range(int(seconds * RATE)))).tobytes()


def tool(*command: str) -> str:
    return subprocess.run(command, capture_output=True, text=True, stdin=subprocess.DEVNULL,
                          timeout=5).stdout


async def link(source: str, target: str):
    """There is no session manager here, so streams wait for this."""
    for _ in range(250):
        if source in tool("pw-link", "-o").split() and target in tool("pw-link", "-i").split():
            tool("pw-link", source, target)
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"no ports to link {source} to {target}")


@unittest.skipUnless(all(map(shutil.which, ("pipewire", "pw-cli", "pw-link", "pw-cat", "pw-record"))),
                     "needs the PipeWire tools")
class PrivateGraph(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="tux-pw.", dir="/tmp")
        self.addCleanup(temp.cleanup)
        folder = Path(temp.name)
        (folder / "run").mkdir(mode=0o700)
        conf = folder / "graph.conf"
        conf.write_text(CONFIG)
        patch = mock.patch.dict(os.environ, {
            "XDG_RUNTIME_DIR": str(folder / "run"), "PIPEWIRE_RUNTIME_DIR": str(folder / "run"),
            "PIPEWIRE_REMOTE": GRAPH, "HOME": str(folder), "XDG_CONFIG_HOME": str(folder / "config"),
            "PIPEWIRE_PROPS": "{ %s }" % (PORTS % "false")})
        patch.start()
        self.addCleanup(patch.stop)
        os.environ.pop("DBUS_SESSION_BUS_ADDRESS", None)
        graph = subprocess.Popen(["pipewire", "-c", str(conf)], stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
        self.addCleanup(graph.wait, 5)
        self.addCleanup(graph.terminate)
        for _ in range(100):
            if (folder / "run" / GRAPH).exists():
                break
            time.sleep(0.05)
        self.assertIn(f'name: "{GRAPH}"', self.core_info(), "not the private graph")

    @staticmethod
    def core_info() -> str:
        """pw-cli stays open after a one-shot command when stdin is not a terminal."""
        try:
            return subprocess.run(["pw-cli", "info", "0"], capture_output=True, text=True,
                                  stdin=subprocess.DEVNULL, timeout=1).stdout
        except subprocess.TimeoutExpired as e:
            return (e.stdout or b"").decode()

    async def test_tone_goes_through_speaker_and_mic(self):
        mic = Mic()
        await mic.start()
        await link("tux-loop:monitor_MONO", "pw-record:input_MONO")
        received = bytearray()
        listening = True

        async def capture():
            while listening:
                received.extend(await asyncio.wait_for(mic.read(), 5))

        reader = asyncio.create_task(capture())
        levels = []
        speaker = Speaker(on_level=levels.append)
        await speaker.start()
        await link("pw-cat:output_MONO", "tux-loop:playback_MONO")
        await asyncio.sleep(0.2)
        speaker.write(tone(TONE_S))
        started = time.monotonic()
        await speaker.drain()
        drained = time.monotonic() - started
        await asyncio.sleep(0.3)
        listening = False
        await reader
        await mic.stop()

        self.assertEqual(speaker.proc.returncode, 0, "pw-cat should exit by itself at the end of input")
        self.assertLess(drained, TONE_S + 0.5, "paced in real time, without the terminate fallback")
        self.assertEqual(levels[-1], 0.0)
        self.assertGreater(max(levels), 0.5)

        samples = array.array("h", bytes(received[: len(received) // 2 * 2]))
        voiced = [i for i, sample in enumerate(samples) if abs(sample) > 3000]
        self.assertTrue(voiced, "nothing came back through the graph")
        body = samples[voiced[0]:voiced[-1]]
        length = len(body) / RATE
        crossings = sum(a < 0 <= b for a, b in zip(body, body[1:]))
        self.assertAlmostEqual(length, TONE_S, delta=0.15)
        self.assertAlmostEqual(crossings / length, TONE_HZ, delta=3)
        self.assertLess(abs(max(map(abs, body)) - 0.3 * 32767), 0.1 * 32767, "level should survive")
        self.assertGreater(level(body[:2400].tobytes()), 0.3)

    async def test_stop_is_immediate_mid_playback(self):
        speaker = Speaker()
        await speaker.start()
        await link("pw-cat:output_MONO", "tux-loop:playback_MONO")
        speaker.write(bytes(BYTES_PER_S * 5))
        await asyncio.sleep(0.5)
        started = time.monotonic()
        await speaker.stop()
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertIsNotNone(speaker.proc.returncode)


if __name__ == "__main__":
    unittest.main()
