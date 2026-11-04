"""The state machine behind the blob: idle, connecting, listening, thinking, speaking, error.

A job (a voice turn, or speaking some text) is one task that ends at idle however it goes, so
cancelling is always task.cancel() and the cleanup lives in one place. Commands take a lock
because stopping a job is not instant and they must apply in the order they arrived.
"""
import asyncio
import logging
import secrets
import time

from .audio import BYTES_PER_S, Mic, Speaker, level
from .gate import APPROVAL_WAIT_S, MAX_DETAIL_CHARS, MAX_TITLE_CHARS, Ask, printable
from .realtime import SETUP_TIMEOUT_S, Realtime, RealtimeError, sentences, speech

log = logging.getLogger("tuxistant")

FOLLOW_UP_S = 8         # conversation_mode: how long listening reopens after a reply
MAX_LISTEN_S = 60       # the server never heard the end, as in a noisy room
MAX_HOLD_S = 120        # a stuck key must not leave the mic open for ever
REPLY_TIMEOUT_S = 60    # silence from the server while a reply is due
ERROR_HOLD_S = 1.5      # how long the error state shows before idle
CONNECT_LIMIT_S = SETUP_TIMEOUT_S + 5   # past the client's own timeout: this is the backstop
MIN_HOLD_BYTES = int(0.4 * BYTES_PER_S)
MIN_VOICED_BYTES = int(0.12 * BYTES_PER_S)  # more than the thump of the key itself
VOICE_LEVEL = 0.15      # audio.level() of a quiet voice. Whisper invents words for silence
VOICE_GRACE_S = 1.0     # speech_started lags, so a voice right at the timeout gets this long
SELECTION_WAIT_S = 2    # the shell answers at once if it answers at all
FOUND_MEMORY_S = 300    # "open the second one" can come in a new turn, as a hold turn ends at once
NO_CARD = ("I can't show an approval card on this desktop yet, so I didn't do that. Log out and "
           "back in once, then try again.")


def describe(error: Exception) -> str:
    """What to show the user for a failure."""
    if isinstance(error, FileNotFoundError) and error.filename:
        return f"{error.filename} is not installed"
    if isinstance(error, (RealtimeError, OSError)):
        return str(error)
    log.error("turn failed", exc_info=error)
    return "Something went wrong. The log has the details"  # its text could hold anything


class Turn:
    """One connection: a hold turn, or a VAD turn and its conversation follow-ups."""

    def __init__(self, ctl, hold: bool):
        self.ctl, self.hold = ctl, hold
        self.inbox = asyncio.Queue()    # everything the turn reacts to, in order of arrival
        self.rt = ctl.make_realtime(ctl.config, ctl.skills, self.inbox.put_nowait, hold=hold)
        self.rt.confirm, self.rt.selection = ctl.ask, ctl.selection
        if time.monotonic() - ctl.found_at < FOUND_MEMORY_S:
            self.rt.session.found = list(ctl.found)
        self.connecting = None
        self.connected = False
        self.mic_task = None
        self.speaker = None
        self.released = False
        self.aborted = False
        self.closing = False
        self.connect_by = 0.0
        self._new_exchange()

    def _new_exchange(self):
        self.heard, self.replies, self.actions, self.after = "", [], [], []
        self.captured = self.voiced = 0
        self.last_voice = 0.0

    async def run(self):
        ctl = self.ctl
        ctl.set_presence(True)
        ctl.set_state("connecting")
        self.connect_by = time.monotonic() + CONNECT_LIMIT_S
        self.connecting = asyncio.create_task(self._connect())
        self._open_mic()    # before the session exists: speech during connecting is kept
        patience = ctl.config["listen_timeout_s"]
        outcome = None
        try:
            while await self._exchange(patience):
                self._new_exchange()
                self._open_mic()
                ctl.set_state("listening")
                patience = FOLLOW_UP_S
        except asyncio.CancelledError:
            outcome = "cancelled"
            raise
        except Exception:
            outcome = "error"
            raise
        finally:
            self.aborted = outcome is not None
            try:
                await self._close()
            finally:
                if outcome:     # after closing: by now nothing more can start or arrive
                    self._take_late_events(show=False)
                    self._record(outcome)

    async def _exchange(self, patience: float) -> bool:
        """One request and reply. Returns whether to listen again."""
        ctl = self.ctl
        start = time.monotonic()
        deadline = start + patience if self.connected else self.connect_by
        while True:
            kind, *args = await self._next(deadline)
            now = time.monotonic()
            match kind:
                case "ready":
                    self.connected = True
                    if self.released:
                        await self.rt.commit()
                        deadline = now + REPLY_TIMEOUT_S
                    else:
                        ctl.set_state("listening")
                        deadline = now + (MAX_HOLD_S if self.hold else patience)
                case "speech_started":
                    deadline = now + MAX_LISTEN_S
                case "committed" if not self.hold and ctl.state == "listening":
                    await self._to_thinking()
                    deadline = now + REPLY_TIMEOUT_S
                case "heard":
                    self.heard = args[0]
                    ctl.emit("Transcript", args[0], True)
                    if not self.hold and ctl.state == "listening":
                        await self._to_thinking()
                    deadline = now + REPLY_TIMEOUT_S
                case "hold_end" if not self.released:
                    if not await self._release():
                        return False
                    deadline = now + REPLY_TIMEOUT_S if self.connected else self.connect_by
                case "tool":
                    self.actions.append(args[0])
                    deadline = now + REPLY_TIMEOUT_S
                case "action":
                    deadline = now + REPLY_TIMEOUT_S
                case "busy":
                    deadline = now + args[0]
                case "speak":
                    self.after.append(args[0])
                case "audio":
                    await self._play(args[0])
                    deadline = now + REPLY_TIMEOUT_S
                case "reply":
                    self.replies.append(args[0])
                    ctl.emit("Reply", args[0])
                case "done":
                    await self._close_mic()
                    if self.speaker:
                        await self.speaker.drain()
                        self.speaker = None
                    await self._speak_after()
                    self._take_late_events()
                    self._record("ok")
                    return not self.hold and ctl.config["conversation_mode"]
                case "quiet":
                    return False
                case "failed":
                    raise RealtimeError(args[0])
                case "timeout":
                    if ctl.state != "listening":
                        raise RealtimeError("The server stopped responding")
                    if self.hold:
                        if not await self._release():
                            return False
                        deadline = now + REPLY_TIMEOUT_S
                    elif now - self.last_voice < VOICE_GRACE_S and now - start < MAX_LISTEN_S:
                        deadline = now + VOICE_GRACE_S
                    else:
                        return False

    async def _next(self, deadline: float | None) -> tuple:
        timeout = None if deadline is None else max(0.0, deadline - time.monotonic())
        try:
            return await asyncio.wait_for(self.inbox.get(), timeout)
        except TimeoutError:
            return ("timeout",)

    def _take_late_events(self, show=True):
        """What is left in the inbox, for the history. The transcript can trail the reply."""
        while not self.inbox.empty():
            kind, *args = self.inbox.get_nowait()
            if kind == "heard":
                self.heard = args[0]
                if show:
                    self.ctl.emit("Transcript", args[0], True)
            elif kind == "reply":
                self.replies.append(args[0])
                if show:
                    self.ctl.emit("Reply", args[0])
            elif kind == "tool":
                self.actions.append(args[0])

    async def _speak_after(self):
        """What the app says itself once the model is done, each in its own voice."""
        ctl = self.ctl
        for item in self.after:
            ctl.set_presence(True)      # it is worth reading too, even after a hold was released
            ctl.emit("Reply", item.text)
            self.replies.append(item.text)
            if not (item.voice or item.model):
                continue
            try:
                for sentence in sentences(item.text):
                    await self._play(await speech(ctl.config, sentence, item.voice, item.model))
            except RealtimeError as e:      # the text is on screen, so the request still worked
                log.warning("could not speak the result: %s", e)
        self.after = []
        if self.speaker:
            await self.speaker.drain()
            self.speaker = None

    async def _to_thinking(self):
        await self._close_mic()     # so the assistant does not hear itself
        self.ctl.set_state("thinking")

    async def _release(self) -> bool:
        """The user let go. Returns False if the hold was too short or quiet to send."""
        self.released = True
        await self._close_mic()
        if self.ctl.config["hide_on_release"]:
            self.ctl.set_presence(False)
        if self.captured < MIN_HOLD_BYTES or self.voiced < MIN_VOICED_BYTES:
            return False
        self.ctl.set_state("thinking")
        if self.connected:
            await self.rt.commit()
        return True

    async def _connect(self):
        try:
            await self.rt.connect()
            self.inbox.put_nowait(("ready",))
        except Exception as e:     # anything else here would leave the turn waiting for ever
            self.inbox.put_nowait(("failed", describe(e)))

    def _open_mic(self):
        self.mic_task = asyncio.create_task(self._pump_mic())

    async def _pump_mic(self):
        mic = self.ctl.make_mic()
        try:
            await mic.start()
            while True:
                pcm = await mic.read()
                if not pcm:
                    raise RealtimeError("The microphone stopped working")
                loudness = level(pcm)
                self.ctl.emit("Level", loudness)
                self.captured += len(pcm)
                if loudness >= VOICE_LEVEL:
                    self.voiced += len(pcm)
                    self.last_voice = time.monotonic()
                await self.rt.send_audio(pcm)
        except Exception as e:
            self.inbox.put_nowait(("failed", describe(e)))
        finally:
            await mic.stop()

    async def _close_mic(self):
        task, self.mic_task = self.mic_task, None
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            self.ctl.emit("Level", 0.0)

    async def _play(self, pcm: bytes):
        if self.speaker is None:
            await self._close_mic()
            self.speaker = self.ctl.new_speaker()
            await self.speaker.start()
            self.ctl.set_state("speaking")
        self.speaker.write(pcm)

    def _record(self, status: str):
        """A turn that never got anywhere leaves no row; a failure always does."""
        if self.rt.session.found:
            self.ctl.found = list(self.rt.session.found)
            self.ctl.found_at = time.monotonic()
        reply = " ".join(self.replies)
        if self.heard or reply or self.actions or status == "error":
            self.ctl.history.add(self.heard, reply, self.actions, status)
            self.ctl.emit("HistoryChanged")
        self._new_exchange()

    async def _close(self):
        self.closing = True
        if self.speaker:
            await self.speaker.stop()
        await self._close_mic()
        self.connecting.cancel()
        await asyncio.gather(self.connecting, return_exceptions=True)
        await self.rt.close(abort=self.aborted)


class Controller:
    def __init__(self, config, history, skills, emit=None,
                 mic_factory=Mic, speaker_factory=Speaker, realtime_factory=Realtime):
        self.config, self.history, self.skills = config, history, skills
        self.emit = emit or (lambda name, *args: None)
        self.make_mic, self.make_speaker = mic_factory, speaker_factory
        self.make_realtime = realtime_factory
        self.state = "idle"
        self.presence = False
        self.locked = False
        self.job = None
        self.turn = None
        self.lock = asyncio.Lock()
        self.approval = None    # (id, future) of the card on screen
        self.reading = None     # (id, future) of the selection the shell was asked for
        self.face = False       # an extension that can draw cards has said hello
        self.found, self.found_at = [], 0.0     # file.find's latest results, across turns

    @property
    def active(self) -> bool:
        return self.job is not None and not self.job.done()

    def set_state(self, state: str):
        if state != self.state:
            self.state = state
            self.emit("StateChanged", state)

    def set_presence(self, visible: bool):
        if visible != self.presence:
            self.presence = visible
            self.emit("Presence", visible)

    def new_speaker(self):
        return self.make_speaker(on_level=lambda value: self.emit("Level", value))

    async def ask(self, ask: Ask) -> bool:
        """Puts an approval card on screen and waits for a click. No answer, a cancel or a lock
        all mean no. One card at a time: actions in a turn run one after the other. With nothing
        to draw the card, the user hears why rather than waiting for a card that never comes."""
        if self.locked or self.approval:
            return False
        if not self.face:
            raise RuntimeError(NO_CARD)
        request = secrets.token_hex(16)
        answer = asyncio.get_running_loop().create_future()
        self.approval = (request, answer)
        was_visible = self.presence
        self.set_presence(True)
        self.emit("Approval", request, printable(ask.title, MAX_TITLE_CHARS),
                  printable(ask.detail, MAX_DETAIL_CHARS), ask.level, APPROVAL_WAIT_S)
        try:
            return await asyncio.wait_for(answer, APPROVAL_WAIT_S)
        except TimeoutError:
            return False
        finally:
            self.approval = None
            self.emit("ApprovalClosed", request)
            self.set_presence(was_visible)

    def answer(self, request: str, allow: bool):
        """An unknown id is a card that has gone: a late click must not approve the next one."""
        if self.approval and secrets.compare_digest(self.approval[0], request):
            if not self.approval[1].done():
                self.approval[1].set_result(bool(allow))

    async def selection(self) -> str:
        """The text selected on screen, or on the clipboard, as the shell sees it."""
        if self.locked or self.reading:
            return ""
        request = secrets.token_hex(16)
        text = asyncio.get_running_loop().create_future()
        self.reading = (request, text)
        self.emit("SelectionWanted", request)
        try:
            return await asyncio.wait_for(text, SELECTION_WAIT_S)
        except TimeoutError:
            return ""
        finally:
            self.reading = None

    def selection_text(self, request: str, text: str):
        if self.reading and secrets.compare_digest(self.reading[0], request):
            if not self.reading[1].done():
                self.reading[1].set_result(text)

    async def toggle(self):
        async with self.lock:
            if self.active:
                await self._stop()
            elif not self.locked:
                self._start_turn(hold=False)

    async def hold_start(self):
        async with self.lock:
            await self._stop()
            if not self.locked:
                self._start_turn(hold=True)

    async def hold_end(self):
        async with self.lock:
            if self.active and self.turn and self.turn.hold:
                self.turn.inbox.put_nowait(("hold_end",))

    async def cancel(self):
        async with self.lock:
            await self._stop()

    async def set_locked(self, locked: bool):
        """The shell disables the extension at lock, so a turn would have no blob and no way to
        stop it. Nothing listens or speaks on a locked screen, wherever the request came from."""
        self.locked = locked
        if locked:
            await self.cancel()

    async def say(self, text: str, voice: str | None = None) -> asyncio.Task | None:
        """Speaks text, replacing whatever was going on. The job's result is what went wrong."""
        async with self.lock:
            await self._stop()
            if self.locked:
                return None
            self.job = asyncio.create_task(
                self._run(lambda: self._say(text, voice or self.config["voice"])))
            return self.job

    def _start_turn(self, hold: bool):
        self.turn = Turn(self, hold)
        self.job = asyncio.create_task(self._run(self.turn.run))

    async def _stop(self):
        if self.active:
            # A turn that is closing ends by itself, and soon. A cancel would land in the middle
            # of the cleanup and leave the socket or the microphone open.
            if not (self.turn and self.turn.closing):
                self.job.cancel()
            await asyncio.wait({self.job})
        self.turn = None

    async def _run(self, work) -> str | None:
        """Every job ends here. No failure in a job may reach the event loop."""
        try:
            await work()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            problem = describe(e)
            self.turn = None    # it has closed, and _stop may cancel the wait below
            self.emit("Error", problem)
            self.set_state("error")
            try:
                await asyncio.sleep(ERROR_HOLD_S)
            except asyncio.CancelledError:
                pass
            return problem
        finally:
            self.turn = None
            self.set_state("idle")
            self.set_presence(False)

    async def _say(self, text: str, voice: str):
        self.set_presence(True)
        self.set_state("thinking")
        speaker = None
        try:
            self.emit("Reply", text)
            for sentence in sentences(text):
                pcm = await speech(self.config, sentence, voice)
                if speaker is None:
                    speaker = self.new_speaker()
                    await speaker.start()
                    self.set_state("speaking")
                speaker.write(pcm)
            if speaker:
                await speaker.drain()
                speaker = None
        finally:
            if speaker:
                await speaker.stop()
