"""The state machine behind the blob: idle, connecting, listening, thinking, speaking, error.

A job (a voice turn, or speaking some text) is one task that ends at idle however it goes, so
cancelling is always task.cancel() and the cleanup lives in one place. Commands take a lock
because stopping a job is not instant and they must apply in the order they arrived.
"""
import asyncio
import json
import logging
import math
import secrets
import time

from . import knowledge, planner
from .audio import MIN_HOLD_BYTES, MIN_VOICED_BYTES, VOICE_LEVEL, Mic, Speaker, level
from .dictation import NOTHING_HEARD, Dictation, clean
from .gate import APPROVAL_WAIT_S, MAX_DETAIL_CHARS, MAX_TITLE_CHARS, Ask, Speak, printable
from .realtime import SETUP_TIMEOUT_S, Realtime, RealtimeError, sentences, speech, transcribe

log = logging.getLogger("tuxistant")

FOLLOW_UP_S = 8         # conversation_mode: how long listening reopens after a reply
MAX_LISTEN_S = 60       # the server never heard the end, as in a noisy room
MAX_HOLD_S = 120        # a stuck key must not leave the mic open for ever
REPLY_TIMEOUT_S = 60    # silence from the server while a reply is due
ERROR_HOLD_S = 1.5      # how long the error state shows before idle
CONNECT_LIMIT_S = SETUP_TIMEOUT_S + 5   # past the client's own timeout: this is the backstop
VOICE_GRACE_S = 1.0     # speech_started lags, so a voice right at the timeout gets this long
SELECTION_WAIT_S = 2    # the shell answers at once if it answers at all
NOTICE_WAIT_S = 3       # past this a notice is no use, and the turn can't read while it waits
FOUND_MEMORY_S = 300    # "open the second one" can come in a new conversation too
FOLLOW_ON_S = 120       # a hold this soon after the last carries on its conversation
QUESTION_HINT_S = 10    # how long a question stays on screen once the turn is over
ANSWER_GRACE_S = 2      # a press of Fn just as the hint fades still answers it
WAKE_CHECK_S = 1.0      # how soon after waking a conversation past its time is ended
ANSWER_HINT = "Hold Fn to answer"
NO_CARD = ("I can't show an approval card on this desktop yet, so I didn't do that. Log out and "
           "back in once, then try again.")
NO_TYPING = "Dictation needs the new desktop extension. Log out and back in once, then try again."


def describe(error: Exception) -> str:
    """What to show the user for a failure."""
    if isinstance(error, FileNotFoundError) and error.filename:
        return f"{error.filename} is not installed"
    if isinstance(error, (RealtimeError, OSError)) and str(error):    # a bare TimeoutError has none
        log.warning("turn failed: %s", error)   # the screen shows it too, but a terminal has no screen
        return str(error)
    log.error("turn failed", exc_info=error)
    return "Something went wrong. The log has the details"  # its text could hold anything


def _ignore(_event):
    pass


def clock() -> float:
    """The boot clock, which counts the time the computer was suspended. The conversation's
    windows are timed on it: the monotonic clock, and asyncio's with it, stops in a suspend, and a
    conversation or a question must not carry on into the next morning."""
    return time.clock_gettime(time.CLOCK_BOOTTIME)


def _asleep() -> float:
    """How long the computer has been suspended since it booted."""
    return clock() - time.monotonic()


class Talk:
    """A conversation that outlives its hold turns: the client, with its connection if that is
    still open, and its Session, with what the conversation touched. See "Follow-ups" in
    docs/CONTRACT.md."""

    def __init__(self, rt):
        self.rt = rt
        self.asked = False      # its last reply was a question
        self.asked_at = 0.0     # when that question went on screen, by clock()
        self.question = ""      # and what it was, to put it back
        self.kept_at = 0.0      # when its last hold turn ended, by clock()
        self.timer = None       # the task that ends it after FOLLOW_ON_S

    def over(self) -> bool:
        return clock() - self.kept_at >= FOLLOW_ON_S


class Turn:
    """One request at a time on one conversation: a hold turn, or a VAD turn and its follow-ups."""

    def __init__(self, ctl, hold: bool, talk: Talk | None = None, pressed: float | None = None):
        """pressed: when Fn went down for this hold, by clock()."""
        self.ctl, self.hold = ctl, hold
        self.inbox = asyncio.Queue()    # everything the turn reacts to, in order of arrival
        self.skills = ctl.skills    # one Skills for the whole turn: what it offers, it can run
        self.talk = talk or Talk(
            ctl.make_realtime(ctl.config, self.skills, self.inbox.put_nowait, hold=hold))
        self.rt = self.talk.rt
        self.rt.on_event, self.rt.parked = self.inbox.put_nowait, False
        self.rt.confirm, self.rt.selection = ctl.ask, ctl.selection
        if talk is None and clock() - ctl.found_at < FOUND_MEMORY_S:
            self.rt.session.found = list(ctl.found)
        # The words of this hold answer a task that asked the user something, but only if Fn
        # went down while its question was on screen: later words may be about anything. That is
        # the press, not HoldStart, which comes up to fn_hold_ms later. A task whose server, key
        # or Cloud setting has changed since is dropped, and the words go the usual way.
        pressed = clock() if pressed is None else pressed
        on_screen = bool(talk and talk.asked
                         and pressed - talk.asked_at < QUESTION_HINT_S + ANSWER_GRACE_S)
        self.for_task = on_screen and planner.resumable(self.rt.session.waiting,
                                                          self.skills.config)
        if talk and not self.for_task:
            self.rt.session.waiting = None
        self.last_reply = None      # of this turn, if it said anything
        # What Tux itself last said: the voice model's reply, or a task's answer that asked. The
        # app's own Speak texts never count, as a mail or a web page could end in "?".
        self.question = None
        self.ends_talk = False      # a card was answered no
        self.connecting = None
        self.connected = False
        self.held = bytearray()     # a hold turn's audio, which never goes to the session
        self.asking = None
        self.answering = False      # a fact is on its way, which needs nothing from the session
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
                await self._close(keep=self.hold and not self.aborted and not self.ends_talk)
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
                    if self.connected:
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
                case "notice":
                    await self._notice(args[0])
                case "audio":
                    await self._play(args[0])
                    deadline = now + REPLY_TIMEOUT_S
                case "reply":
                    self.replies.append(args[0])
                    self.question = args[0]
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
                    if self.heard:
                        self._record("ok")
                    return False
                case "failed" if not self.answering:
                    if self.after:  # what the app already found is not lost with the reply
                        await self._close_mic()
                        await self._speak_after()
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
        if not self.inbox.empty():      # however late this gets read, what arrived still counts
            return self.inbox.get_nowait()
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
                self.question = args[0]
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

    async def _notice(self, item):
        """Said while an action runs, so a long silence doesn't sound like something broke."""
        try:
            pcm = await asyncio.wait_for(speech(self.ctl.config, item.text, item.voice, item.model),
                                         NOTICE_WAIT_S)
        except (TimeoutError, RealtimeError) as e:
            log.warning("could not speak the notice: %r", e)
            return
        try:
            await self._play(pcm)
            await self.speaker.drain()
        except OSError as e:    # the answer is what matters, and it can still be shown
            log.warning("could not play the notice: %s", e)
            await self.speaker.stop()
        self.speaker = None
        self.ctl.set_state("thinking")

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
        self.asking = asyncio.create_task(self._ask())
        return True

    async def _ask(self):
        """A hold turn, once the user has let go. The server replies to audio the moment it's
        committed, whatever it's told, and there'd be no stopping it. So the audio stays here, the
        words are got separately, from the same Whisper in the same time, and the voice model
        hears them only if they aren't a question of fact. See knowledge.py for why."""
        config = self.ctl.config
        try:
            heard = clean(await transcribe(config, bytes(self.held)))
            if not heard:
                self.inbox.put_nowait(("quiet",))
            else:
                self.inbox.put_nowait(("heard", heard))
                if self.for_task:
                    self.answering = True
                    await self._answer_task(heard)
                elif await knowledge.is_fact(heard, config):
                    self.answering = True
                    _, result = await self.rt.session.execute(
                        "knowledge.answer", json.dumps({"question": heard}))
                    if "error" in result:
                        raise RealtimeError(result["error"])
                    self.inbox.put_nowait(("done",))
                else:
                    await self.connecting   # a failure to connect reaches the inbox by itself
                    if self.rt.ready:
                        await self.rt.say(heard)
        except Exception as e:
            self.answering = False
            self.inbox.put_nowait(("failed", describe(e)))

    async def _answer_task(self, heard: str):
        """Hands the words to the task that asked for them, through the same gates as ever. Only
        this sets session.replying, so a model calling task.run can't pass its words off as the
        user's. The app says the task's answer, as nothing asked the voice model for one."""
        session = self.rt.session
        session.replying = True
        try:
            _, result = await session.execute(planner.ACTION, json.dumps({"goal": heard}))
        finally:
            session.replying = False
        if "error" in result:
            raise RealtimeError(result["error"])
        if result.get("answer"):
            answer = str(result["answer"])
            self.question = answer if session.waiting is not None else None
            self.inbox.put_nowait(("speak", Speak(answer, self.ctl.config["voice"], "")))
        self.inbox.put_nowait(("done",))

    async def _connect(self):
        try:
            if self.rt.skills is not self.skills:   # reloaded since: the new tools need a new session
                await self.rt.close()
                self.rt.use(self.skills)
            if self.rt.alive and not self.rt.outdated:
                await self.rt.still_there()     # a link that died quietly gets no words
            if not self.rt.alive or self.rt.outdated:
                # Whatever is left of a connection the server dropped, or one made with a server,
                # key, voice or tools that have changed since: the words must go where the
                # settings say.
                await self.rt.close()
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
                if self.hold:
                    self.held += pcm
                else:
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
            self.ctl.found_at = clock()
        reply = " ".join(self.replies)
        if self.replies:
            self.last_reply = self.replies[-1]
        if self.heard or reply or self.actions or status == "error":
            self.ctl.history.add(self.heard, reply, self.actions, status)
            self.ctl.emit("HistoryChanged")
        self._new_exchange()

    async def _close(self, keep=False):
        """keep leaves the connection open for the next hold, if it still is."""
        self.closing = True
        if self.speaker:
            await self.speaker.stop()
        await self._close_mic()
        self.connecting.cancel()
        tasks = [task for task in (self.asking, self.connecting) if task]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.rt.on_event, self.rt.parked = _ignore, True    # nothing is asked of it meanwhile
        if not (keep and self.rt.alive):
            await self.rt.close(abort=self.aborted)


class _Fixed:
    """A Skills that never reloads, standing in for a SkillHost."""

    def __init__(self, skills):
        self.skills = skills

    def current(self):
        return self.skills


class Controller:
    def __init__(self, config, history, skills, emit=None,
                 mic_factory=Mic, speaker_factory=Speaker, realtime_factory=Realtime):
        self.config, self.history = config, history
        self.host = skills if callable(getattr(skills, "current", None)) else _Fixed(skills)
        self.emit = emit or (lambda name, *args: None)
        self.make_mic, self.make_speaker = mic_factory, speaker_factory
        self.make_realtime = realtime_factory
        self.state = "idle"
        self.presence = False
        self.locked = False
        self.job = None
        self.turn = None
        self.dictation = None
        self.deliveries = []    # dictated audio on its way to the window, oldest first
        self.lock = asyncio.Lock()
        self.approval = None    # (id, future) of the card on screen
        self.reading = None     # (id, future) of the selection the shell was asked for
        self.typing = None      # (id, future) of the words the shell was asked to type
        self.face = False       # an extension that can draw cards has said hello
        self.typist = False     # and one that can type
        self.found, self.found_at = [], 0.0     # file.find's latest results, across turns
        self.talk = None        # the conversation the next hold carries on, while no turn has it

    @property
    def skills(self):
        """The skills as they are now. A reload swaps them, so a turn takes this once."""
        return self.host.current()

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
            allowed = await asyncio.wait_for(answer, APPROVAL_WAIT_S)
        except TimeoutError:
            return False
        else:
            if not allowed and self.turn:   # a no means stop, so nothing carries on after it
                self.turn.ends_talk = True
            return allowed
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

    def typed(self, request: str, how: str):
        if self.typing and secrets.compare_digest(self.typing[0], request):
            if not self.typing[1].done():
                self.typing[1].set_result(how)

    @property
    def dictating(self) -> bool:
        return self.active and self.dictation is not None

    def going(self, go: str | None) -> bool:
        """Whether the go with this id is the one listening. None means whichever is."""
        return self.dictating and go in (None, self.dictation.id)

    async def dictate_start(self, mode: str | None = None) -> str | None:
        """mode: how it will be stopped, if known now. See Dictation.set_mode. Returns the id of
        the go it started, or None if it started none, as one was already listening."""
        async with self.lock:
            if not self.config["dictation_enabled"] or self.dictating:
                return None
            await self._stop()
            if self.locked:
                return None
            if not self.typist:
                self.emit("Error", NO_TYPING)
                return None
            if mode:
                self._drop_question()
            self.dictation = Dictation(self, mode)
            self.job = asyncio.create_task(self._run(self.dictation.run))
            return self.dictation.id

    async def dictate_end(self, go: str | None = None):
        """go: only that go, for Fn, which may end only the go it started."""
        async with self.lock:
            if self.going(go):
                self._drop_question()
                self.dictation.finish()

    async def dictate_toggle(self, mode: str | None = None):
        if self.dictating:
            await self.dictate_end()
        else:
            await self.dictate_start(mode)

    async def dictate_mode(self, mode: str, go: str | None = None):
        """Fn knows only after the press that started it whether it was a tap or a hold."""
        if self.going(go):
            self.dictation.set_mode(mode)
            if self.dictation.mode:     # now it is dictation, not the start of a Fn shortcut
                self._drop_question()

    def deliver(self, go: Dictation):
        """Apart from the job, so that the next go or a talk hold can start while these words are
        at the server. They reach the window in the order they were said. Only Cancel, a lock or
        losing the shell stops them."""
        before = self.deliveries[-1] if self.deliveries else None
        task = asyncio.create_task(self._deliver(go, before))
        self.deliveries.append(task)
        task.add_done_callback(self.deliveries.remove)

    async def _deliver(self, go: Dictation, before: asyncio.Task | None):
        try:
            words = clean(await transcribe(self.config, bytes(go.pcm)))
            if before:
                await asyncio.wait({before})
            if words:
                await go.type(words + " ")
            else:
                self.emit("Error", NOTHING_HEARD)
        except Exception as e:
            self.emit("Error", describe(e))

    def _drop_words(self):
        for task in self.deliveries:
            task.cancel()

    async def shell_gone(self):
        """With nothing to show it or take the words, dictation has to stop."""
        async with self.lock:
            self._drop_words()
            if self.active and self.dictation:
                await self._stop()

    async def toggle(self):
        """While dictating, the talk shortcut ends the go as its own key would, and starts no turn."""
        async with self.lock:
            if self.dictating:
                await self._end_dictation()
            elif self.active:
                await self._stop()
                await self._end_talk()
            elif not self.locked:
                await self._end_talk()
                self._start_turn(hold=False)

    async def hold_start(self, held: float = 0.0):
        """held: how long ago Fn went down, as the helper only says so after fn_hold_ms."""
        pressed = clock() - held
        async with self.lock:
            await self._end_dictation()
            await self._stop()
            if self.talk and self.talk.over():  # a suspend kept its timer from ending it
                await self._end_talk()
            if not self.locked:
                self._start_turn(hold=True, pressed=pressed)

    async def hold_end(self):
        async with self.lock:
            if self.active and self.turn and self.turn.hold:
                self.turn.inbox.put_nowait(("hold_end",))

    async def cancel(self):
        async with self.lock:
            await self._stop()
            self._drop_words()
            await self._end_talk()
            self._forget()

    async def drop_go(self, go: str):
        """A Fn shortcut on the press that started go: it was never dictation, so that go is
        dropped, and only it. Another go, as one from the shortcut, the words of earlier gos
        still at the server and the conversation are left alone. If Fn hadn't yet said tap or
        hold, the question its listening hid is put back."""
        async with self.lock:
            if not self.going(go):
                return
            unsure = self.dictation.mode is None
            await self._stop()
            if unsure:
                self._ask_again()

    async def set_locked(self, locked: bool):
        """The shell disables the extension at lock, so a turn would have no blob and no way to
        stop it. Nothing listens or speaks on a locked screen, wherever the request came from."""
        self.locked = locked
        if locked:
            await self.cancel()

    async def say(self, text: str, voice: str | None = None) -> asyncio.Task | None:
        """Speaks text, replacing whatever was going on. The job's result is what went wrong."""
        async with self.lock:
            await self._end_dictation()
            await self._stop()
            if self.locked:
                return None
            self._drop_question()   # its words replace the question on screen
            self.job = asyncio.create_task(
                self._run(lambda: self._say(text, voice or self.config["voice"])))
            return self.job

    async def say_when_free(self, text: str, lead: bytes = b"",
                            heard=None) -> asyncio.Task | None:
        """For a timer or a reminder. Unlike say(), it never stops a turn, a card or dictation
        to speak: None means not now, and the caller asks again. lead is a sound for before
        the words, and heard() is called once words have gone to the speaker."""
        async with self.lock:
            if self.locked or self.active:
                return None
            self._drop_question()   # its words replace the question on screen
            self.job = asyncio.create_task(
                self._run(lambda: self._say(text, self.config["voice"], lead, heard)))
            return self.job

    async def sound(self, pcm: bytes) -> None:
        """Plays a sound that has no words, such as a timer's chime. It is the one thing heard
        on a locked screen: it tells nobody anything, nothing listens, and it ends by itself."""
        async with self.lock:
            if not self.active:
                self.job = asyncio.create_task(self._run(lambda: self._sound(pcm)))

    async def _end_dictation(self):
        """A go can run for minutes. Ended by anything but Cancel, a lock or losing the shell, it
        keeps its words, or says why there were none, as if its own key had stopped it."""
        if self.dictating:
            self._drop_question()
            self.dictation.finish()
            await asyncio.wait({self.job})

    def _start_turn(self, hold: bool, pressed: float | None = None):
        talk, self.talk = (self.talk, None) if hold else (None, self.talk)
        if talk and talk.timer:
            talk.timer.cancel()
        self.turn = Turn(self, hold, talk, pressed)
        self.job = asyncio.create_task(self._converse(self.turn))

    async def _converse(self, turn: Turn):
        await self._run(turn.run)
        if turn.ends_talk:
            self._forget()
        elif turn.hold:
            self._keep(turn)

    def _keep(self, turn: Turn):
        """After a hold turn however it ended: the conversation waits for the next hold. Sent
        after the turn's idle and Presence, so that nothing takes the question down."""
        talk = self.talk = turn.talk
        talk.kept_at = clock()
        if turn.last_reply is not None:
            talk.asked = turn.question is not None and planner.asks(turn.question)
            if talk.asked and not self.locked:
                talk.asked_at, talk.question = clock(), turn.question
                self.emit("FollowUp", turn.question, ANSWER_HINT, QUESTION_HINT_S)
        elif not turn.aborted:  # it heard nothing: the hold took the question down, so it's back
            self._ask_again()
        if not talk.asked:  # a task's question nobody was left with is no question
            talk.rt.session.waiting = None
        talk.timer = asyncio.create_task(self._expire(talk))

    async def _expire(self, talk: Talk):
        """Ends the talk FOLLOW_ON_S after its last hold by clock(), looking every WAKE_CHECK_S,
        as asyncio's own clock stops in a suspend. The shell's clock stops too, so on waking it
        would show the question for the time it had left when the computer went to sleep: it
        hears what is really left, or that the question has gone."""
        slept = _asleep()
        while (left := talk.kept_at + FOLLOW_ON_S - clock()) > 0:
            await asyncio.sleep(min(left, WAKE_CHECK_S))
            if _asleep() - slept > 1.0:
                slept = _asleep()
                self._woke(talk)
        async with self.lock:
            if self.talk is talk:
                talk.timer = None   # this task: ending the talk must not cancel it half way
                await self._end_talk()

    async def _end_talk(self):
        """The next hold starts a new conversation, with nothing carried over."""
        talk, self.talk = self.talk, None
        if talk:
            if talk.timer:
                talk.timer.cancel()
            if talk.asked:
                self.emit("FollowUp", "", "", 0)
            await talk.rt.close()

    def _drop_question(self):
        """A go, or words the app speaks, take the question off the screen, so the next hold is
        no answer to it. The conversation itself carries on."""
        talk = self.talk
        if talk and talk.asked:
            talk.asked = False
            talk.rt.session.waiting = None
            self.emit("FollowUp", "", "", 0)

    def _ask_again(self):
        """The shell took the question down when a go or a hold started listening. It comes back
        for the time it had left, and a hold in that time still answers it."""
        talk = self.talk
        if talk and talk.asked and not self.locked:
            left = math.ceil(QUESTION_HINT_S - (clock() - talk.asked_at))
            if left > 0:
                self.emit("FollowUp", talk.question, ANSWER_HINT, left)

    def _woke(self, talk: Talk):
        """While a go or words the app speaks are up, the shell shows no question anyway."""
        if self.talk is not talk or not talk.asked or self.active:
            return
        if clock() - talk.asked_at < QUESTION_HINT_S:
            self._ask_again()
        else:
            self._drop_question()

    def _forget(self):
        """Stop, a lock or a no: the next conversation can't open what this one found either."""
        self.found, self.found_at = [], 0.0

    async def _stop(self):
        if self.active:
            # A turn that is closing ends by itself, and soon. A cancel would land in the middle
            # of the cleanup and leave the socket or the microphone open.
            if not (self.turn and self.turn.closing):
                self.job.cancel()
            await asyncio.wait({self.job})
        self.turn = self.dictation = None

    async def _run(self, work) -> str | None:
        """Every job ends here. No failure in a job may reach the event loop."""
        try:
            await work()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            problem = describe(e)
            self.turn = self.dictation = None   # it has closed, and _stop may cancel the wait below
            self.emit("Error", problem)
            self.set_state("error")
            try:
                await asyncio.sleep(ERROR_HOLD_S)
            except asyncio.CancelledError:
                pass
            return problem
        finally:
            self.turn = self.dictation = None
            self.set_state("idle")
            self.set_presence(False)

    async def _say(self, text: str, voice: str, lead: bytes = b"", heard=None):
        self.set_presence(True)
        self.set_state("thinking")
        speaker = None
        try:
            self.emit("Reply", text)
            if lead:    # heard at once, and even if the server can't be reached for the words
                speaker = await self._speaking()
                speaker.write(lead)
            for sentence in sentences(text):
                try:
                    pcm = await speech(self.config, sentence, voice)
                except Exception:
                    # What was handed over plays to its end: stop() would cut the chime, or
                    # the sentence before, as it clears the buffer. A key press still cuts it,
                    # as a cancel is no Exception.
                    if speaker:
                        await speaker.drain()
                        speaker = None
                    raise
                speaker = speaker or await self._speaking()
                speaker.write(pcm)
                if heard:
                    heard()
            if speaker:
                await speaker.drain()
                speaker = None
        finally:
            if speaker:
                await speaker.stop()

    async def _speaking(self):
        speaker = self.new_speaker()
        await speaker.start()
        self.set_state("speaking")
        return speaker

    async def _sound(self, pcm: bytes):
        speaker = self.make_speaker(on_level=lambda _value: None)    # no blob to move
        try:
            await speaker.start()
            speaker.write(pcm)
            await speaker.drain()
            speaker = None
        finally:
            if speaker:
                await speaker.stop()
