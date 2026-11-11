"""LocalAI client: the realtime session and speech synthesis. Protocol: docs/CONTRACT.md."""
import array
import asyncio
import base64
import http.client
import io
import json
import re
import secrets
import sys
import textwrap
import urllib.error
import urllib.parse
import urllib.request
import wave

import websockets
from websockets.exceptions import ConnectionClosed, InvalidStatus, WebSocketException

from .audio import BYTES_PER_S, RATE
from .config import valid_key
from .gate import APP_SAYS, APPROVAL_WAIT_S, Ask, Session

SETUP_TIMEOUT_S = 30        # a cold pipeline takes about 6 s to say session.created
HTTP_TIMEOUT_S = 60
CLOSE_TIMEOUT_S = 2         # commands wait for a turn to stop, so a dead link must not hold them
MAX_TOOL_ROUNDS = 6         # a small model can keep calling tools forever
MAX_PENDING_BYTES = 30 * BYTES_PER_S
MAX_SPEECH_CHARS = 400      # long inputs can run the 8 GB GPU out of memory
REPLY_WAIT_AFTER_ANSWER_S = 60
NOT_COMMITTED = "input_not_committed"
LOST = "Lost the connection to the server"


class RealtimeError(Exception):
    """A problem worth showing to the user: the message is human-readable."""


class AuthError(RealtimeError):
    def __init__(self):
        super().__init__("The server rejected the API key")


def _urls(server: str) -> tuple[str, str]:
    """(http, ws) base URLs. server is host:port; an explicit scheme also works."""
    scheme, _, host = server.strip().rstrip("/").rpartition("://")
    secure = scheme.lower() in ("https", "wss")
    return (f"https://{host}", f"wss://{host}") if secure else (f"http://{host}", f"ws://{host}")


def _bearer(config) -> str:
    """Checked here too: config.json can hold anything, and an exception about a bad header
    would carry the key with it to the log and to the screen."""
    if not valid_key(config["api_key"]):
        raise RealtimeError("The API key in the settings is not valid")
    return f"Bearer {config['api_key']}"


class _Connect(websockets.connect):
    def process_redirect(self, exc):
        """A redirect would take the key and the microphone to wherever it points."""
        return exc


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None     # the 3xx then surfaces as an HTTPError


# No proxies either, like the websocket: a proxy would see the key on every request.
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirects)


def _message(error: dict) -> str:
    return f"Server error: {error.get('message') or error.get('code') or 'unknown'}"


class Realtime:
    """One session. Reports what happens by calling on_event with a tuple:

    ("speech_started",) ("committed",) ("heard", text) ("action", record) ("audio", pcm)
    ("reply", text) ("done",) ("quiet",) ("failed", message) ("busy", seconds) ("speak", Speak)
    ("notice", Speak)

    "done" fires once per request, after the last response: a response that only called tools
    does not count. "quiet" means nothing was committed, which is not an error. "busy" means an
    action needs that long, so the silence is not the server's fault. "notice" is for saying now,
    while an action runs, where "speak" waits for the end.

    confirm and selection are the controller's, set by the turn. Left alone, every Ask is refused.
    """

    def __init__(self, config, skills, on_event, hold=False):
        self.config, self.skills, self.on_event, self.hold = config, skills, on_event, hold
        self.ws = None
        self.reader = None
        self.ready = False
        self.pending = bytearray()      # audio captured before the session could take it
        self.seen_calls = set()
        self.called = False             # the response in progress ran a tool
        self.to_report = False          # a result of this reply is for the model to put into words
        self.owed = False               # and so is one of an earlier reply, until it speaks
        self.rounds = 0
        self.confirm = None
        self.selection = None
        self.record = None              # the action in progress
        self.session = Session(skills, self._confirm, lambda s: on_event(("busy", s)),
                               self._selection, self._act, lambda s: on_event(("notice", s)))

    async def _confirm(self, ask: Ask) -> bool:
        """Says yes only if the controller did, and keeps the answer in the history."""
        self.on_event(("busy", APPROVAL_WAIT_S + 5))
        allowed = False
        try:
            allowed = bool(self.confirm and await self.confirm(ask))
        finally:    # a card ended by a cancel or a lock was still shown
            if self.record is not None:
                self.record.setdefault("asked", []).append(
                    {"title": ask.title, "detail": ask.detail, "level": ask.level,
                     "allowed": allowed})
        self.on_event(("busy", REPLY_WAIT_AFTER_ANSWER_S))
        return allowed

    async def _selection(self) -> str:
        return await self.selection() if self.selection else ""

    async def connect(self):
        """Opens the session and returns once the server has confirmed the setup."""
        server = self.config["server"]
        url = "{}/v1/realtime?model={}".format(
            _urls(server)[1], urllib.parse.quote(self.config["pipeline"], safe=""))
        authorization = _bearer(self.config)
        try:
            async with asyncio.timeout(SETUP_TIMEOUT_S):
                # max_size: response.done repeats the whole reply as base64.
                self.ws = await _Connect(
                    url, additional_headers={"Authorization": authorization},
                    max_size=None, compression=None, ping_interval=None, close_timeout=1,
                    open_timeout=None, proxy=None)
                await self._until("session.created")
                await self._send({"type": "session.update", "session": self._session()})
                await self._until("session.updated")
                # send_audio keeps buffering until this has emptied, so nothing is out of order.
                while self.pending:
                    chunk = bytes(self.pending[:BYTES_PER_S])
                    del self.pending[:BYTES_PER_S]
                    await self._append(chunk)
        except TimeoutError:
            raise RealtimeError(f"Timed out waiting for the server at {server}") from None
        except InvalidStatus as e:
            if e.response.status_code in (401, 403):
                raise AuthError() from None
            raise RealtimeError(f"Server error: HTTP {e.response.status_code}") from None
        except ConnectionClosed:
            raise RealtimeError(LOST) from None
        except (OSError, ValueError, WebSocketException):    # ValueError: a bad port or host name
            raise RealtimeError(f"Cannot reach the server at {server}") from None
        self.ready = True
        self.reader = asyncio.create_task(self._read())

    def _session(self) -> dict:
        audio = {"output": {"voice": self.config["voice"]}}
        if self.hold:
            audio["input"] = {"turn_detection": None}
        return {"type": "realtime", "instructions": self.config["instructions"],
                "tools": self.skills.tools(), "audio": audio}

    async def _until(self, wanted: str):
        async for raw in self.ws:
            try:
                event = json.loads(raw)
                kind = event.get("type")
            except (ValueError, AttributeError):
                raise RealtimeError("Bad message from the server") from None
            if kind == "error":
                raise RealtimeError(_message(event.get("error") or {}))
            if kind == wanted:
                return
        raise RealtimeError(LOST)

    async def _send(self, event: dict):
        try:
            await self.ws.send(json.dumps(event))
        except ConnectionClosed:
            raise RealtimeError(LOST) from None

    async def _append(self, pcm: bytes):
        await self._send({"type": "input_audio_buffer.append",
                          "audio": base64.b64encode(pcm).decode()})

    async def send_audio(self, pcm: bytes):
        """Audio sent before session.created is lost, so it waits here until then."""
        if self.ready:
            await self._append(pcm)
        else:
            self.pending += pcm
            del self.pending[:-MAX_PENDING_BYTES]

    async def say(self, text: str):
        """Hold turn: what the user said, as words. A reply to it is just like a reply to audio."""
        await self._send({"type": "conversation.item.create", "item": {
            "type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}})
        await self._send({"type": "response.create"})

    async def close(self, abort=False):
        """Ends the session. abort also stops whatever the server is still generating."""
        if self.reader:
            self.reader.cancel()
            await asyncio.gather(self.reader, return_exceptions=True)
        if self.ws:
            try:
                async with asyncio.timeout(CLOSE_TIMEOUT_S):
                    if abort and self.ready:
                        await self.ws.send(json.dumps({"type": "response.cancel"}))
                    await self.ws.close()
            except TimeoutError:
                self.ws.transport.abort()
            except (ConnectionClosed, OSError):
                pass

    async def _read(self):
        try:
            async for raw in self.ws:
                await self._handle(json.loads(raw))
            reason = LOST
        except ConnectionClosed:
            reason = LOST
        except RealtimeError as e:
            reason = str(e)
        except Exception as e:      # whatever it was, the turn has to hear that the reader is gone
            reason = f"Bad message from the server: {e!r}"
        self.on_event(("failed", reason))

    async def _handle(self, event: dict):
        kind = event.get("type")
        if kind == "input_audio_buffer.speech_started":
            self.on_event(("speech_started",))
        elif kind == "input_audio_buffer.committed":
            self.on_event(("committed",))
        elif kind == "conversation.item.input_audio_transcription.completed":
            self.on_event(("heard", event["transcript"]))
        elif kind == "response.function_call_arguments.done":
            await self._run_tool(event["name"], event["arguments"], event["call_id"])
        elif kind in ("response.output_audio.delta", "response.audio.delta"):
            self.on_event(("audio", base64.b64decode(event["delta"])))
        elif kind in ("response.output_audio_transcript.done", "response.audio_transcript.done"):
            self.owed = False       # it has spoken, whatever else this reply holds
            self.on_event(("reply", event["transcript"]))
        elif kind == "response.done":
            await self._response_done(event["response"])
        elif kind == "error":
            error = event.get("error") or {}
            quiet = error.get("code") == NOT_COMMITTED
            self.on_event(("quiet",) if quiet else ("failed", _message(error)))

    async def _run_tool(self, name: str, arguments: str, call_id: str):
        if call_id in self.seen_calls:
            return
        self.seen_calls.add(call_id)
        self.called = True
        _, result = await self._act(name, arguments)
        self.to_report = self.to_report or not (isinstance(result, dict) and result.get(APP_SAYS))
        await self._send({"type": "conversation.item.create", "item": {
            "type": "function_call_output", "call_id": call_id, "output": json.dumps(result)}})

    async def _act(self, name: str, arguments: str) -> tuple[str, dict]:
        """One action, whether the model or the planner asked for it: each gets its own record.
        Announced first: a cancel does not stop the thread, and what ran belongs in the history.
        result stays None if the turn ends before the action does."""
        record = {"name": self.skills.action_name(name), "arguments": arguments, "result": None}
        self.on_event(("tool", record))
        outer, self.record = self.record, record    # so a card lands on the action that asked
        try:
            action, record["result"] = await self.skills.execute(name, arguments, self.session)
        finally:
            self.record = outer
        for speak in self.session.take_after():
            self.on_event(("speak", speak))
        self.on_event(("action", record))
        return action, record["result"]

    async def _response_done(self, response: dict):
        status = response.get("status")
        error = (response.get("status_details") or {}).get("error") or {}
        owed, self.to_report = self.to_report or self.owed, False
        if error.get("code") == NOT_COMMITTED:
            self.owed = False
            self.on_event(("quiet",))
        elif status == "failed":
            self.owed = False
            self.on_event(("failed", _message(error)))
        elif self.called and status == "completed" and owed:
            # Tool outputs are in, so this was not the end of the request. Asking once here,
            # not per call, keeps two calls in one response from starting two responses. What is
            # owed stays owed until the model has spoken: its next reply may be another tool.
            self.called, self.owed = False, True
            self.rounds += 1
            if self.rounds >= MAX_TOOL_ROUNDS:
                self.owed = False
                self.on_event(("failed", "The assistant kept calling tools without answering"))
            else:
                await self._send({"type": "response.create"})
        else:
            # The request ends here: the model spoke, or the app says all of it itself (see
            # APP_SAYS), or the response was cut short. Nothing is owed after it.
            self.called = self.owed = False
            self.rounds = 0
            self.on_event(("done",))


def sentences(text: str) -> list[str]:
    """Splits text for /v1/audio/speech by sentence, then by length for run-on ones."""
    parts = []
    for part in re.split(r"(?<=[.!?])\s+|\n+", text.strip()):
        parts += textwrap.wrap(part, MAX_SPEECH_CHARS)
    return parts


def _request(config, path: str, body: dict | bytes | None = None,
             content_type: str = "application/json") -> bytes:
    headers = {"Authorization": _bearer(config), "Content-Type": content_type}
    data = body if isinstance(body, bytes) else json.dumps(body).encode() if body else None
    try:
        request = urllib.request.Request(_urls(config["server"])[0] + path, data, headers)
        with _opener.open(request, timeout=HTTP_TIMEOUT_S) as response:
            return response.read()
    except urllib.error.HTTPError as e:
        e.close()
        if e.code in (401, 403):
            raise AuthError() from None
        raise RealtimeError(f"Server error: HTTP {e.code}") from None
    except (OSError, ValueError, http.client.HTTPException):
        raise RealtimeError(f"Cannot reach the server at {config['server']}") from None


def _resample(pcm: bytes, rate: int) -> bytes:
    """Mono 16-bit PCM to RATE. Piper voices are 16 or 22.05 kHz; Kokoro is already 24 kHz."""
    samples = array.array("h", pcm)
    if sys.byteorder == "big":
        samples.byteswap()
    if rate == RATE or len(samples) < 2:
        return pcm
    out = array.array("h", bytes(2 * (len(samples) * RATE // rate)))
    last = len(samples) - 1
    for i in range(len(out)):
        position = i * rate / RATE
        left = min(int(position), last)
        right = min(left + 1, last)
        out[i] = int(samples[left] + (samples[right] - samples[left]) * (position - left))
    if sys.byteorder == "big":
        out.byteswap()
    return out.tobytes()


def _synthesise(config, text: str, voice: str, model: str | None = None) -> bytes:
    data = _request(config, "/v1/audio/speech",
                    {"model": model or config["tts_model"], "voice": voice, "input": text})
    try:
        with wave.open(io.BytesIO(data)) as wav:
            if (wav.getnchannels(), wav.getsampwidth()) != (1, 2):
                raise RealtimeError("Speech is not mono 16-bit")
            return _resample(wav.readframes(wav.getnframes()), wav.getframerate())
    except (wave.Error, EOFError):
        raise RealtimeError("The server did not return audio") from None


async def speech(config, text: str, voice: str, model: str | None = None) -> bytes:
    """Synthesises one sentence as PCM for the Speaker."""
    return await asyncio.to_thread(_synthesise, config, text, voice, model)


def _wav(pcm: bytes) -> bytes:
    audio = io.BytesIO()
    with wave.open(audio, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(RATE)
        out.writeframes(pcm)
    return audio.getvalue()


def _transcribe(config, pcm: bytes) -> str:
    edge = secrets.token_hex(16)
    body = b"".join([
        f'--{edge}\r\nContent-Disposition: form-data; name="model"\r\n\r\n'.encode(),
        config["transcribe_model"].encode(), b"\r\n",
        f'--{edge}\r\nContent-Disposition: form-data; name="file"; filename="speech.wav"\r\n'
        "Content-Type: audio/wav\r\n\r\n".encode(),
        _wav(pcm), f"\r\n--{edge}--\r\n".encode()])
    reply = _request(config, "/v1/audio/transcriptions", body,
                     f"multipart/form-data; boundary={edge}")
    try:
        text = json.loads(reply)["text"]
    except (ValueError, KeyError, TypeError):
        text = None
    if not isinstance(text, str):
        raise RealtimeError("The server did not return any text")
    return text


async def transcribe(config, pcm: bytes) -> str:
    """What was said in PCM from the Mic, with no model involved."""
    return await asyncio.to_thread(_transcribe, config, pcm)


def list_models(config) -> list[str]:
    return [m["id"] for m in json.loads(_request(config, "/v1/models")).get("data", [])]
