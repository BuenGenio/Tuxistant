"""The D-Bus face of the daemon. See "D-Bus" in docs/CONTRACT.md."""
import asyncio
import json
import os
import signal as signals
import sys
import time
from pathlib import Path

from dbus_fast import DBusError, Message, MessageType, NameFlag, RequestNameReply
from dbus_fast.aio import MessageBus
from dbus_fast.service import ServiceInterface, method, signal

from . import BUS_NAME, BUS_PATH, phone, themes, voices
from .audio import Mic, Speaker
from .config import DATA_DIR, Config
from .controller import Controller
from .fnclient import FnClient
from .history import History
from .skills import Skills

LEVEL_INTERVAL_S = 1 / 30
LIST_THEMES_TIMEOUT_S = 25      # themes.TRIAL_BUDGET_S plus one trial
PREVIEW_TEXT = "Hi, I'm Tux. This is how I sound."
INVALID = f"{BUS_NAME}.Error.InvalidArgument"
FAILED = f"{BUS_NAME}.Error.Failed"
# The shell's own name for this. org.gnome.ScreenSaver is a separate process in front of it.
SHIELD_NAME, SHIELD_PATH, SHIELD_INTERFACE = (
    "org.gnome.Shell.ScreenShield", "/org/gnome/ScreenSaver", "org.gnome.ScreenSaver")
SHELL_NAME = "org.gnome.Shell"
SHELL_ONLY = ("Hello", "Answer", "SelectionText")   # a click has to come from the desktop itself
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


class Daemon(ServiceInterface):
    def __init__(self, config, history, skills, **audio):
        super().__init__(BUS_NAME)
        self.config, self.history, self.skills = config, history, skills
        self.controller = Controller(config, history, skills, emit=self.send, **audio)
        self.last_level = 0.0
        self.children = set()
        self.shell_owner = None     # the unique bus name of the running GNOME Shell

    def set_shell(self, owner: str | None):
        """A new shell has no card code until its extension says so."""
        self.shell_owner = owner
        self.controller.face = False

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
        getattr(self, name)(*args)

    async def launch_settings(self):
        env = dict(os.environ)
        package_root = str(Path(__file__).resolve().parent.parent)
        env["PYTHONPATH"] = os.pathsep.join(filter(None, [package_root, env.get("PYTHONPATH")]))
        child = await asyncio.create_subprocess_exec(
            sys.executable, "-P", "-m", "tuxistant", "settings", env=env)
        task = asyncio.create_task(child.wait())    # reaps it
        self.children.add(task)
        task.add_done_callback(self.children.discard)

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
    def GetState(self) -> "s":
        return self.controller.state

    @method()
    def GetConfig(self) -> "s":
        return json.dumps(self.config.public())

    @method()
    def SetConfig(self, key: "s", json_value: "s"):
        try:
            self.config.set(key, json.loads(json_value))
        except (KeyError, TypeError, ValueError) as e:
            raise DBusError(INVALID, str(e.args[0])) from None
        self.ConfigChanged(key)

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
        return json.dumps(self.skills.listing())

    @method()
    async def ListPhones(self) -> "s":
        return json.dumps(await asyncio.to_thread(phone.devices))

    @method()
    def Hello(self, cards: "i"):
        self.controller.face = cards >= 1

    @method()
    def CanAsk(self) -> "b":
        return self.controller.face

    @method()
    def Answer(self, id: "s", allow: "b"):
        self.controller.answer(id, allow)

    @method()
    def SelectionText(self, id: "s", text: "s"):
        self.controller.selection_text(id, text)

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
    def Approval(self, id, title, detail, level, seconds) -> "ssssi":
        return [id, title, detail, level, seconds]

    @signal()
    def ApprovalClosed(self, id) -> "s":
        return id

    @signal()
    def SelectionWanted(self, id) -> "s":
        return id


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
    await watch_shell(bus, daemon)
    bus.export(BUS_PATH, daemon)
    reply = await bus.request_name(BUS_NAME, NameFlag.DO_NOT_QUEUE)
    return reply == RequestNameReply.PRIMARY_OWNER


async def watch_lock(bus, controller):
    """Tells the controller when the screen shield goes up or down. A bus with no GNOME Shell on
    it never locks; if a shell turns up later its signals still arrive."""
    shield = bus.get_proxy_object(SHIELD_NAME, SHIELD_PATH, SHIELD_XML).get_interface(
        SHIELD_INTERFACE)
    shield.on_active_changed(controller.set_locked)
    try:
        await controller.set_locked(await shield.call_get_active())
    except DBusError:
        pass


async def run(mic_factory=Mic, speaker_factory=Speaker) -> int:
    config = Config()
    history = History(DATA_DIR / "history.db", config["history_limit"])
    daemon = Daemon(config, history, Skills(config=config), mic_factory=mic_factory,
                    speaker_factory=speaker_factory)
    bus = await MessageBus().connect()
    if not await serve(bus, daemon):
        print("tuxistant: the daemon is already running", file=sys.stderr)
        bus.disconnect()
        return 1
    controller = daemon.controller
    fn = asyncio.create_task(FnClient(config, controller.hold_start, controller.hold_end).run())
    lock = asyncio.create_task(watch_lock(bus, controller))
    stop = asyncio.Event()
    for number in (signals.SIGINT, signals.SIGTERM):
        asyncio.get_running_loop().add_signal_handler(number, stop.set)
    waits = {asyncio.create_task(stop.wait()), asyncio.create_task(bus.wait_for_disconnect())}
    await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
    for task in (fn, lock, *waits):
        task.cancel()
    await controller.cancel()
    await asyncio.gather(fn, lock, *waits, return_exceptions=True)
    bus.disconnect()
    return 0
