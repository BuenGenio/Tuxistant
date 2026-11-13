"""The D-Bus face of the daemon. See "D-Bus" in docs/CONTRACT.md."""
import asyncio
import json
import logging
import os
import signal as signals
import sys
import time
from pathlib import Path

from dbus_fast import DBusError, Message, MessageType, NameFlag, RequestNameReply
from dbus_fast.aio import MessageBus
from dbus_fast.service import ServiceInterface, method, signal

from . import BUS_NAME, BUS_PATH, calendars, phone, themes, timers, voices
from .audio import Mic, Speaker
from .config import DATA_DIR, Config
from .controller import Controller
from .fnclient import FnClient
from .history import History
from .skillhost import SkillHost

log = logging.getLogger(__name__)
LOCK_QUERY_S = 5

LEVEL_INTERVAL_S = 1 / 30
LIST_THEMES_TIMEOUT_S = 25      # themes.TRIAL_BUDGET_S plus one trial
PREVIEW_TEXT = "Hi, I'm Tux. This is how I sound."
INVALID = f"{BUS_NAME}.Error.InvalidArgument"
FAILED = f"{BUS_NAME}.Error.Failed"
# The shell's own name for this. org.gnome.ScreenSaver is a separate process in front of it.
SHIELD_NAME, SHIELD_PATH, SHIELD_INTERFACE = (
    "org.gnome.Shell.ScreenShield", "/org/gnome/ScreenSaver", "org.gnome.ScreenSaver")
SHELL_NAME = "org.gnome.Shell"
# These have to come from the desktop. Fn reaches dictation inside this process.
SHELL_ONLY = ("Hello", "Answer", "SelectionText", "Typed", "DictateStart", "DictateEnd",
              "DictateToggle")
DENIED = f"{BUS_NAME}.Error.Denied"
BUS_DAEMON = "org.freedesktop.DBus"
BUS_DAEMON_XML = f"""<node><interface name="{BUS_DAEMON}">
<method name="GetNameOwner"><arg type="s" direction="in"/><arg type="s" direction="out"/></method>
<signal name="NameOwnerChanged"><arg type="s"/><arg type="s"/><arg type="s"/></signal>
</interface></node>"""
SHIELD_XML = f"""<node><interface name="{SHIELD_INTERFACE}">
<method name="GetActive"><arg type="b" direction="out"/></method>
<signal name="ActiveChanged"><arg type="b"/></signal>
</interface></node>"""
START_GRACE_S = 15      # at login the sound server and the shell are still coming up
RESTART_S = 5           # after the scheduler fails. Sooner would be a loop if it fails again


class Daemon(ServiceInterface):
    def __init__(self, config, history, skills, **audio):
        """skills: a SkillHost, or a fixed Skills for tests."""
        super().__init__(BUS_NAME)
        self.config, self.history = config, history
        self.host = skills if callable(getattr(skills, "reload", None)) else None
        self.controller = Controller(config, history, skills, emit=self.send, **audio)
        self.last_level = 0.0
        self.children = set()
        self.shell_owner = None     # the unique bus name of the running GNOME Shell
        self.bus = None

    def set_shell(self, owner: str | None):
        """A new shell has no card or typing code until its extension says so."""
        self.shell_owner = owner
        self.controller.face = self.controller.typist = False
        self.drop_dictation()

    def drop_dictation(self):
        ctl = self.controller
        if ctl.dictation or ctl.deliveries:
            self.keep(ctl.shell_gone())

    def guard(self, message: Message) -> Message | None:
        """Runs before dispatch. Matched by name alone: a call may leave out the interface and
        still reach the method, so checking the interface would leave a way round this."""
        if (message.message_type == MessageType.METHOD_CALL and message.member in SHELL_ONLY
                and message.sender != self.shell_owner):
            return Message.new_error(message, DENIED, "Only GNOME Shell can do that")
        return None

    def send(self, name: str, *args):
        """Emits a signal for the controller. A zero level always passes so the blob can settle."""
        if name == "Level":
            now = time.monotonic()
            if args[0] and now - self.last_level < LEVEL_INTERVAL_S:
                return
            self.last_level = now
        if name == "TypeText":
            self.to_shell(name, "ssb", *args)
            return
        getattr(self, name)(*args)

    def to_shell(self, name: str, signature: str, *body):
        """What someone dictated is for the shell, not for everyone on the bus."""
        if self.bus and self.shell_owner:
            self.bus.send(Message(message_type=MessageType.SIGNAL, destination=self.shell_owner,
                                  path=BUS_PATH, interface=BUS_NAME, member=name,
                                  signature=signature, body=list(body)))

    def keep(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.children.add(task)
        task.add_done_callback(self.children.discard)

    def set_config(self, key: str, value):
        self.config.set(key, value)
        self.ConfigChanged(key)
        if self.host:   # a skill given this setting hears the new value at once
            self.keep(self.host.config_changed(key))

    def reload_skills(self) -> str:
        return json.dumps(self.host.reload() if self.host else {})

    def skills_changed(self):
        self.SkillsChanged()

    async def launch_settings(self):
        env = dict(os.environ)
        package_root = str(Path(__file__).resolve().parent.parent)
        env["PYTHONPATH"] = os.pathsep.join(filter(None, [package_root, env.get("PYTHONPATH")]))
        child = await asyncio.create_subprocess_exec(
            sys.executable, "-P", "-m", "tuxistant", "settings", env=env)
        self.keep(child.wait())     # reaps it

    @method()
    async def Toggle(self):
        await self.controller.toggle()

    @method()
    async def HoldStart(self):
        await self.controller.hold_start()

    @method()
    async def HoldEnd(self):
        await self.controller.hold_end()

    @method()
    async def Cancel(self):
        await self.controller.cancel()

    @method()
    async def DictateStart(self):
        await self.controller.dictate_start()

    @method()
    async def DictateEnd(self):
        await self.controller.dictate_end()

    @method()
    async def DictateToggle(self):
        await self.controller.dictate_toggle()

    @method()
    def GetState(self) -> "s":
        return self.controller.state

    @method()
    def GetConfig(self) -> "s":
        return json.dumps(self.config.public())

    @method()
    def SetConfig(self, key: "s", json_value: "s"):
        try:
            self.set_config(key, json.loads(json_value))
        except (KeyError, TypeError, ValueError) as e:
            raise DBusError(INVALID, str(e.args[0])) from None

    @method()
    def ListVoices(self) -> "s":
        return json.dumps(voices.catalog())

    @method()
    async def PreviewVoice(self, voice: "s"):
        job = await self.controller.say(PREVIEW_TEXT, voice)
        if job and (problem := await job):
            raise DBusError(FAILED, problem)

    @method()
    async def Say(self, text: "s"):
        await self.controller.say(text)

    @method()
    async def ListThemes(self) -> "s":
        # In a thread: it reads files people shared and test-compiles shaders, and Cancel has to
        # keep working meanwhile.
        try:
            found, problems = await asyncio.wait_for(asyncio.to_thread(themes.discover),
                                                     LIST_THEMES_TIMEOUT_S)
        except TimeoutError:
            raise DBusError(FAILED, "Listing the themes took too long") from None
        return json.dumps({"themes": found, "problems": problems})

    @method()
    def PreviewTheme(self, id: "s"):
        self.ThemePreview(id)

    @method()
    def ListSkills(self) -> "s":
        return json.dumps(self.controller.skills.listing())

    @method()
    def ReloadSkills(self) -> "s":
        return self.reload_skills()

    @method()
    async def ListPhones(self) -> "s":
        return json.dumps(await asyncio.to_thread(phone.devices))

    @method()
    async def ListCalendars(self) -> "s":
        return json.dumps(await asyncio.to_thread(calendars.writable))

    @method()
    def Hello(self, level: "i"):
        self.controller.face = level >= 1
        self.controller.typist = level >= 2
        if level < 2:
            self.drop_dictation()

    @method()
    def CanAsk(self) -> "b":
        return self.controller.face

    @method()
    def CanType(self) -> "b":
        return self.controller.typist

    @method()
    def Answer(self, id: "s", allow: "b"):
        self.controller.answer(id, allow)

    @method()
    def SelectionText(self, id: "s", text: "s"):
        self.controller.selection_text(id, text)

    @method()
    def Typed(self, id: "s", how: "s"):
        self.controller.typed(id, how)

    @method()
    def GetHistory(self, limit: "i") -> "s":
        return json.dumps(self.history.recent(limit))

    @method()
    def ClearHistory(self):
        self.history.clear()
        self.HistoryChanged()

    @method()
    async def OpenSettings(self):
        await self.launch_settings()

    @signal()
    def StateChanged(self, state) -> "s":
        return state

    @signal()
    def Presence(self, visible) -> "b":
        return visible

    @signal()
    def Level(self, value) -> "d":
        return value

    @signal()
    def Transcript(self, text, final) -> "sb":
        return [text, final]

    @signal()
    def Reply(self, text) -> "s":
        return text

    @signal()
    def Error(self, message) -> "s":
        return message

    @signal()
    def ConfigChanged(self, key) -> "s":
        return key

    @signal()
    def ThemePreview(self, id) -> "s":
        return id

    @signal()
    def HistoryChanged(self):
        pass

    @signal()
    def SkillsChanged(self):
        pass

    @signal()
    def Approval(self, id, title, detail, level, seconds) -> "ssssi":
        return [id, title, detail, level, seconds]

    @signal()
    def ApprovalClosed(self, id) -> "s":
        return id

    @signal()
    def SelectionWanted(self, id) -> "s":
        return id

    @signal()
    def DictationStarted(self, id) -> "s":
        return id

    @signal()
    def TypeText(self, id, text, only_copy) -> "ssb":
        return [id, text, only_copy]


async def watch_shell(bus, daemon):
    """Keeps track of who the shell is, so only it can answer a card."""
    names = bus.get_proxy_object(BUS_DAEMON, f"/{BUS_DAEMON.replace('.', '/')}",
                                 BUS_DAEMON_XML).get_interface(BUS_DAEMON)
    names.on_name_owner_changed(
        lambda name, _old, new: daemon.set_shell(new or None) if name == SHELL_NAME else None)
    try:
        daemon.set_shell(await names.call_get_name_owner(SHELL_NAME))
    except DBusError:
        pass    # no shell on this bus, at least not yet


async def serve(bus, daemon) -> bool:
    """Puts the daemon on the bus. False if another one already owns the name. The guard and the
    shell's name come first: a call can arrive the moment the name is ours."""
    bus.add_message_handler(daemon.guard)
    daemon.bus = bus
    await watch_shell(bus, daemon)
    bus.export(BUS_PATH, daemon)
    reply = await bus.request_name(BUS_NAME, NameFlag.DO_NOT_QUEUE)
    return reply == RequestNameReply.PRIMARY_OWNER


async def watch_lock(bus, controller):
    """Tells the controller when the screen shield goes up or down. A bus with no GNOME Shell on
    it never locks; if a shell turns up later its signals still arrive. Every failure, the
    setup's too, leaves speech locked: tick goes on after it, and locked starts as False."""
    try:
        shield = bus.get_proxy_object(SHIELD_NAME, SHIELD_PATH, SHIELD_XML).get_interface(
            SHIELD_INTERFACE)
        shield.on_active_changed(controller.set_locked)
        async with asyncio.timeout(LOCK_QUERY_S):
            await controller.set_locked(await shield.call_get_active())
    except DBusError as error:
        absent = error.type in ("org.freedesktop.DBus.Error.ServiceUnknown",
                                "org.freedesktop.DBus.Error.NameHasNoOwner")
        await controller.set_locked(not absent)
        if not absent:
            log.warning("Cannot read the screen lock; keeping speech locked", exc_info=True)
    except Exception:
        await controller.set_locked(True)
        log.warning("Cannot read the screen lock; keeping speech locked", exc_info=True)


async def tick(store, crier, lock_known) -> None:
    """Runs the timers. Not before it is known whether the screen is locked: a daemon that
    starts on a locked screen with a reminder overdue would otherwise say it aloud. And what
    came due while the daemon was away waits a little longer, so that at login there is
    something to play it and something to show its notification."""
    # However it ended. watch_lock locks speech whenever it fails, before anything else can
    # go wrong, so going on after a failure here still says nothing on a locked screen.
    await asyncio.wait({lock_known})
    if not lock_known.cancelled() and lock_known.exception():
        log.error("The screen lock check failed", exc_info=lock_known.exception())
    if store.due(time.time()):
        await asyncio.sleep(START_GRACE_S)
    while True:
        try:
            await timers.run(store, crier.announce, note=crier.note)
        except Exception:   # a timer nobody hears for the rest of the session is no timer
            log.exception("The timer scheduler failed, so it starts again in %s seconds",
                          RESTART_S)
            await asyncio.sleep(RESTART_S)


def _timers_stopped(task):
    if not task.cancelled() and (error := task.exception()) is not None:
        log.error("The timer scheduler stopped", exc_info=(type(error), error, error.__traceback__))


async def run(mic_factory=Mic, speaker_factory=Speaker) -> int:
    config = Config()
    history = History(DATA_DIR / "history.db", config["history_limit"])
    host = SkillHost(config)
    daemon = Daemon(config, history, host, mic_factory=mic_factory,
                    speaker_factory=speaker_factory)
    host.on_change = daemon.skills_changed
    await host.start()      # before the name is ours, so the first ListSkills has them all
    try:
        return await _serve_until_stopped(daemon)
    finally:
        await host.stop()


async def _serve_until_stopped(daemon) -> int:
    bus = await MessageBus().connect()
    if not await serve(bus, daemon):
        print("tuxistant: the daemon is already running", file=sys.stderr)
        bus.disconnect()
        return 1
    controller = daemon.controller
    # After serve: a second daemon has returned by now, and never touches the first one's file.
    store = timers.Store(DATA_DIR / "timers.json")
    timers.attach(store)
    crier = timers.Announcer(
        say=controller.say_when_free, sound=controller.sound,
        notify=lambda item: timers.notify(bus, item),
        locked=lambda: controller.locked, chimes=lambda: daemon.config["timer_chime"])
    fn = asyncio.create_task(FnClient(
        daemon.config, controller.hold_start, controller.hold_end,
        on_dictate_start=controller.dictate_start, on_dictate_end=controller.dictate_end,
        on_dictate_cancel=controller.cancel).run())
    lock = asyncio.create_task(watch_lock(bus, controller))
    ticking = asyncio.create_task(tick(store, crier, lock))
    ticking.add_done_callback(_timers_stopped)
    stop = asyncio.Event()
    for number in (signals.SIGINT, signals.SIGTERM):
        asyncio.get_running_loop().add_signal_handler(number, stop.set)
    waits = {asyncio.create_task(stop.wait()), asyncio.create_task(bus.wait_for_disconnect())}
    await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
    for task in (ticking, fn, lock, *waits):
        task.cancel()
    await controller.cancel()
    await asyncio.gather(ticking, fn, lock, *waits, return_exceptions=True)
    timers.attach(None)
    bus.disconnect()
    return 0
