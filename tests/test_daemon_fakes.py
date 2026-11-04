"""Shared fakes for the daemon tests: realtime server, speech endpoint, Mic, Speaker.

Event shapes follow what LocalAI v4.12.1 was seen to send. This module has no tests of its own.
"""
import asyncio
import base64
import io
import json
import logging
import math
import struct
import sys
import tempfile
import threading
import unittest
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import websockets

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tuxistant.audio import BYTES_PER_S, RATE  # noqa: E402
from tuxistant.config import Config  # noqa: E402
from tuxistant.controller import Controller  # noqa: E402
from tuxistant.history import History  # noqa: E402
from tuxistant.skills import Skills  # noqa: E402

CHUNK = BYTES_PER_S // 25
logging.getLogger("websockets.server").setLevel(logging.CRITICAL)   # clients hang up on purpose


def tone(seconds: float, amplitude: int = 8000, rate: int = RATE) -> bytes:
    """Loud enough to count as a voice."""
    count = int(seconds * rate)
    return struct.pack(f"<{count}h", *(int(amplitude * math.sin(i / 8)) for i in range(count)))


def silence(seconds: float) -> bytes:
    return bytes(int(seconds * BYTES_PER_S) // 2 * 2)


def wav(pcm: bytes, rate: int = RATE, channels: int = 1) -> bytes:
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return out.getvalue()


class Connection:
    """The server's end of one session, as a script sees it."""

    def __init__(self, ws):
        self.ws = ws
        self.log = []               # every client event except audio, in order
        self.audio = bytearray()
        self.audio_at_commit = None
        self.new_event = asyncio.Condition()

    async def read(self):
        try:
            async for raw in self.ws:
                event = json.loads(raw)
                if event["type"] == "input_audio_buffer.append":
                    self.audio += base64.b64decode(event["audio"])
                else:
                    if event["type"] == "input_audio_buffer.commit":
                        self.audio_at_commit = len(self.audio)
                    self.log.append(event)
                async with self.new_event:
                    self.new_event.notify_all()
        except websockets.ConnectionClosed:
            pass

    def types(self) -> list[str]:
        return [e["type"] for e in self.log]

    async def send(self, kind: str, **fields):
        await self.ws.send(json.dumps({"type": kind, **fields}))

    async def wait_for(self, condition, timeout=5):
        async with asyncio.timeout(timeout):
            async with self.new_event:
                await self.new_event.wait_for(condition)

    async def expect(self, kind: str, count: int = 1) -> dict:
        await self.wait_for(lambda: self.types().count(kind) >= count)
        return [e for e in self.log if e["type"] == kind][count - 1]

    async def heard_audio(self, count: int):
        await self.wait_for(lambda: len(self.audio) >= count)

    # What the server says, in the order it says it.
    async def committed(self, text=None):
        await self.send("input_audio_buffer.speech_stopped", audio_end_ms=1000, item_id="")
        await self.send("input_audio_buffer.committed", item_id="item_user")
        if text is not None:
            await self.transcript(text)

    async def transcript(self, text: str):
        await self.send("conversation.item.input_audio_transcription.completed",
                        item_id="item_user", content_index=0, transcript=text)

    async def tool_call(self, name="time_now", call_id="call_1", arguments="{}"):
        await self.send("response.function_call_arguments.done", name=name, arguments=arguments,
                        call_id=call_id, item_id="item_fn", output_index=0, response_id="r")

    async def done(self, status="completed", **fields):
        await self.send("response.done", response={"id": "r", "object": "realtime.response",
                                                   "status": status, **fields})

    async def reply(self, text="It is noon.", pcm=None, beta=False):
        await self.send("response.created", response={"id": "r", "status": "in_progress"})
        await self.send("response.audio.delta" if beta else "response.output_audio.delta",
                        delta=base64.b64encode(tone(0.2) if pcm is None else pcm).decode())
        await self.send("response.audio_transcript.done" if beta else
                        "response.output_audio_transcript.done", transcript=text)
        await self.done()


class FakeServer:
    """A /v1/realtime endpoint. After the session setup it runs script(connection)."""

    def __init__(self, script=None, key="good", delay=0.0, setup_error=None):
        self.script = script
        self.key = key
        self.delay = delay
        self.setup_error = setup_error
        self.connections = []
        self.sessions = []          # the session object of each session.update
        self.events_before_ready = []

    async def __aenter__(self):
        self.server = await websockets.serve(
            self._serve, "127.0.0.1", 0, max_size=None, process_request=self._auth)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *_):
        self.server.close()
        await self.server.wait_closed()

    def _auth(self, connection, request):
        if request.headers.get("Authorization") != f"Bearer {self.key}":
            return connection.respond(401, "no\n")

    @property
    def conn(self) -> Connection:
        return self.connections[-1]

    async def _serve(self, ws):
        conn = Connection(ws)
        self.connections.append(conn)
        reader = asyncio.create_task(conn.read())
        if self.setup_error:
            await conn.send("error", error=self.setup_error)
            ws.transport.abort()    # as LocalAI does: no close frame
            return
        await asyncio.sleep(self.delay)
        self.events_before_ready = [conn.types(), len(conn.audio)]
        await conn.send("session.created", session={"id": "sess", "model": "tux-realtime"})
        update = await conn.expect("session.update")
        self.sessions.append(update["session"])
        await conn.send("session.updated", session=update["session"])
        if self.script:
            await self.script(conn)
        await reader


class FakeSpeech:
    """The HTTP side of the server: /v1/audio/speech and /v1/models, in its own thread."""

    def __init__(self, key="good", models=("tux-realtime", "tux-voice"), speech_status=200,
                 body=None):
        self.requests = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def respond(self, status, data):
                self.send_response(status)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                if self.headers.get("Authorization") != f"Bearer {key}":
                    return self.respond(401, b"{}")
                self.respond(200, json.dumps({"data": [{"id": m} for m in models]}).encode())

            def do_POST(self):
                request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if self.headers.get("Authorization") != f"Bearer {key}":
                    return self.respond(401, b"{}")
                outer.requests.append(request)
                self.respond(speech_status, wav(tone(0.1)) if body is None else body)

        self.http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.http.server_address[1]
        threading.Thread(target=self.http.serve_forever, args=(0.01,), daemon=True).start()

    def close(self):
        self.http.shutdown()
        self.http.server_close()


class FakeMic:
    """Gives pcm, then silence for ever, or nothing more if block is set."""

    def __init__(self, journal, pcm=b"", block=False, pace=0.002, fail=False):
        self.journal, self.pcm, self.block, self.pace, self.fail = journal, pcm, block, pace, fail
        self.sent = 0

    async def start(self):
        self.journal.append("mic.start")

    async def read(self) -> bytes:
        await asyncio.sleep(self.pace)
        if self.fail:
            return b""
        chunk = self.pcm[self.sent:self.sent + CHUNK]
        self.sent += len(chunk)
        if chunk:
            return chunk
        if self.block:
            await asyncio.Event().wait()
        return bytes(CHUNK)

    async def stop(self):
        self.journal.append("mic.stop")


class FakeSpeaker:
    """Takes play_s to finish, or until gate is set."""

    def __init__(self, journal, on_level, play_s=0.0, gate=None):
        self.journal, self.on_level, self.play_s, self.gate = journal, on_level, play_s, gate
        self.written = bytearray()
        self.drained = self.stopped = False

    async def start(self):
        self.journal.append("speaker.start")

    def write(self, pcm: bytes):
        self.written += pcm
        self.on_level(0.5)

    async def drain(self):
        await asyncio.sleep(self.play_s)
        if self.gate:
            await self.gate.wait()
        self.drained = True
        self.journal.append("speaker.drained")
        self.on_level(0.0)

    async def stop(self):
        self.stopped = True
        self.journal.append("speaker.stop")
        self.on_level(0.0)


class CountingSkills(Skills):
    def __init__(self):
        super().__init__()
        self.ran = []

    def run(self, tool_name, arguments, session=None):
        self.ran.append(tool_name)
        return super().run(tool_name, arguments, session)


class Rig:
    """A Controller on temp files, with fakes for its audio, recording every signal."""

    def __init__(self, folder: Path, port: int, mic_pcm=b"", mic=None, speaker=None, **settings):
        self.config = Config(folder / "config.json")
        self.config.values.update({"server": f"127.0.0.1:{port}", "api_key": "good", **settings})
        self.history = History(folder / "history.db")
        self.skills = CountingSkills()
        self.signals = []
        self.journal = []
        self.mics, self.speakers = [], []
        self.mic_pcm, self.mic_options = mic_pcm, mic or {}
        self.speaker_options = speaker or {}
        self.ctl = Controller(self.config, self.history, self.skills, emit=self.emit,
                              mic_factory=self.new_mic, speaker_factory=self.new_speaker)
        self.ctl.face = True    # as if the extension had said hello

    def emit(self, name, *args):
        self.signals.append((name, *args))

    def new_mic(self):
        self.mics.append(FakeMic(self.journal, self.mic_pcm, **self.mic_options))
        return self.mics[-1]

    def new_speaker(self, on_level):
        self.speakers.append(FakeSpeaker(self.journal, on_level, **self.speaker_options))
        return self.speakers[-1]

    def of(self, name: str) -> list:
        """The arguments of each signal called name."""
        found = [s[1:] for s in self.signals if s[0] == name]
        return [args[0] if len(args) == 1 else args for args in found]

    def index(self, *signal) -> int:
        return self.signals.index(signal)

    async def state(self, wanted: str, timeout=5):
        async with asyncio.timeout(timeout):
            while self.ctl.state != wanted:
                await asyncio.sleep(0.002)

    async def finished(self, timeout=5):
        if self.ctl.job:
            async with asyncio.timeout(timeout):
                await asyncio.wait({self.ctl.job})


class Case(unittest.IsolatedAsyncioTestCase):
    """Gives each test a temp folder."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name)

    async def server(self, *args, **kwargs) -> FakeServer:
        server = FakeServer(*args, **kwargs)
        await server.__aenter__()
        self.addAsyncCleanup(server.__aexit__)
        return server

    def rig(self, port: int, **options) -> Rig:
        rig = Rig(self.folder, port, **options)
        self.addCleanup(rig.history.db.close)
        self.addAsyncCleanup(rig.ctl.cancel)
        return rig
