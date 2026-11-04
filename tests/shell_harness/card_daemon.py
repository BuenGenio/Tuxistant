#!/usr/bin/env python3
"""The approval half of the daemon's bus API, for card.sh.

Sends the real Approval, ApprovalClosed and SelectionWanted signals on the isolated bus and records
what comes back, so an Answer in the report is one that a click really sent. Pointer and keyboard
events are real too, through virtual devices in that shell. Nothing is judged here: the scenarios
write report.json and PNGs, and tests/test_approval_card.py looks at them.
"""
import argparse
import array
import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from dbus_fast.aio import MessageBus
from dbus_fast.service import dbus_method, dbus_signal

import fake_daemon
from fake_daemon import BUS_NAME, BUS_PATH, SETTLE_S, Test
from tuxistant import controller as real_controller, service  # fake_daemon put it on the path
from tuxistant.config import Config
from tuxistant.gate import MAX_DETAIL_CHARS, MAX_TITLE_CHARS, Ask, printable
from tuxistant.history import History
from tuxistant.skills import Skills

ARMED_S = 1.0       # the fade and the wait before Allow works, with some to spare
TAIL = "THE-LAST-WORDS"
SECRET = "the-selected-text-that-must-stay-out-of-logs"
SHORT = ("Move 3 files to the Trash?",
         "/home/user/Documents/report-final.odt\n/home/user/Documents/report-old.odt\n/home/user/notes.txt")
LONG_DETAIL = ("rm -rf " + " ".join(f"/home/user/projects/archive-{i:03}/build" for i in range(110)))[:4000] \
    + f"… (321 more characters) {TAIL}"
LONG = ("Run this command in a terminal?", LONG_DETAIL)
UNBROKEN = ("Run this command in a terminal?", "x" * 3000 + TAIL)
MARKUP = ("Remove <b>this</b> & <i>that</i>?",
          '<span foreground="red">rm</span> -rf "a&b" <b>bold</b> &amp; &lt;tag&gt; </unclosed>\n'
          "<big>not big</big> & a lone < sign")

SNAPSHOT = r"""
const card = ext._approval?._card;
const box = actor => {
    const [x, y] = actor.get_transformed_position();
    const [width, height] = actor.get_transformed_size();
    return {x: Math.round(x), y: Math.round(y), width: Math.round(width), height: Math.round(height)};
};
const focus = global.stage.key_focus;
const state = {
    showing: Boolean(card),
    keyFocus: focus?.name ?? focus?.toString() ?? null,
    caption: ext._caption && {
        visible: ext._caption.actor.visible,
        opacity: ext._caption.actor.opacity,
        heard: ext._caption._heard.text,
        reply: ext._caption._reply.text,
        error: ext._caption._reply.has_style_class_name('tuxistant-caption-error'),
        box: box(ext._caption.actor),
    },
    blob: ext._blob && {visible: ext._blob.visible, box: box(ext._blob.actor)},
    uiGroup: Main.uiGroup.get_n_children(),
    pointerMonitor: global.display.get_current_monitor(),
};
if (!card)
    return state;
const layout = Main.layoutManager;
const monitor = layout.findMonitorForActor(card.actor);
const area = layout.getWorkAreaForMonitor(monitor.index);
const adjustment = card._scroll.vadjustment;
const button = actor => {
    const b = box(actor);
    const hit = global.stage.get_actor_at_pos(Clutter.PickMode.REACTIVE,
        b.x + b.width / 2, b.y + b.height / 2);
    return {...b, label: actor.label, reactive: actor.reactive, focusable: actor.can_focus,
        hit: hit === actor, insensitive: actor.has_style_pseudo_class('insensitive')};
};
const children = Main.uiGroup.get_children();
return {
    ...state,
    id: card.id,
    box: box(card.actor),
    monitor: {index: monitor.index, x: monitor.x, y: monitor.y, width: monitor.width, height: monitor.height},
    area: {x: area.x, y: area.y, width: area.width, height: area.height},
    title: card._title.text,
    titleBox: box(card._title),
    titleScrolls: card._title.get_parent() === card._content,
    detail: card._detail.text,
    detailBox: box(card._scroll),
    detailMarkup: card._detail.clutter_text.use_markup,
    titleMarkup: card._title.clutter_text.use_markup,
    ellipsized: card._detail.clutter_text.get_layout().is_ellipsized(),
    time: card._time.text,
    timeShown: card._time.visible,
    admin: card.actor.has_style_class_name('tuxistant-approval-admin'),
    reactive: card.actor.reactive,
    focusable: card.actor.can_focus,
    opacity: card.actor.opacity,
    more: {visible: card._more.visible, opacity: card._more.opacity, text: card._more.text},
    scroll: {value: adjustment.value, upper: adjustment.upper, page: adjustment.page_size},
    allow: button(card._allow),
    deny: button(card._deny),
    aboveCaption: children.indexOf(card.actor) > children.indexOf(ext._caption.actor),
    aboveBlob: children.indexOf(card.actor) > children.indexOf(ext._blob.actor),
    top: children.at(-1) === card.actor,
};
"""

POINTER = r"""
const driver = Main.extensionManager.lookup('tuxistant-shot@test').stateObj;
const pointer = driver._pointer ??= Clutter.get_default_backend().get_default_seat()
    .create_virtual_device(Clutter.InputDeviceType.POINTER_DEVICE);
const time = () => GLib.get_monotonic_time();
const sleep = ms => new Promise(resolve => GLib.timeout_add(GLib.PRIORITY_DEFAULT, ms, () => {
    resolve();
    return GLib.SOURCE_REMOVE;
}));
"""


class Daemon(fake_daemon.Daemon):
    @dbus_method()
    def Answer(self, id: "s", allow: "b"):
        self.calls.append(["Answer", id, allow])

    @dbus_method()
    def SelectionText(self, id: "s", text: "s"):
        self.calls.append(["SelectionText", id, text])

    @dbus_signal()
    def Approval(self, id, title, detail, level, seconds) -> "ssssi":
        return [id, title, detail, level, seconds]

    @dbus_signal()
    def ApprovalClosed(self, id) -> "s":
        return id

    @dbus_method()
    def Typed(self, id: "s", how: "s"):
        self.calls.append(["Typed", id, how])

    @dbus_signal()
    def SelectionWanted(self, id) -> "s":
        return id

    @dbus_signal()
    def DictationStarted(self, id) -> "s":
        return id

    @dbus_signal()
    def TypeText(self, id, text, only_copy) -> "ssb":
        return [id, text, only_copy]


class Run:
    """What the scenarios do over and over."""

    def __init__(self, daemon, test, args):
        self.daemon, self.test, self.args = daemon, test, args
        self.out = Path(args.out)

    async def card(self, settle=0.0):
        if settle:
            await asyncio.sleep(settle)
        return await self.test.command("eval", code=SNAPSHOT)

    async def shoot(self, name):
        await self.test.command("shoot", path=str(self.out / f"{name}.png"))

    def answers(self):
        return [c for c in self.daemon.calls if c[0] == "Answer"]

    async def click(self, target):
        await self.test.command("click", x=target["x"] + target["width"] / 2,
                                y=target["y"] + target["height"] / 2, button=1)

    async def press_and_hold(self, target, hold_ms):
        x, y = target["x"] + target["width"] / 2, target["y"] + target["height"] / 2
        await self.test.command("eval", code=POINTER + f"""
            pointer.notify_absolute_motion(time(), {x}, {y});
            await sleep(50);
            pointer.notify_button(time(), 1, Clutter.ButtonState.PRESSED);
            await sleep({hold_ms});
            pointer.notify_button(time(), 1, Clutter.ButtonState.RELEASED);
            await sleep(30);
        """)

    async def move_to(self, x, y):
        await self.test.command("eval", code=POINTER + f"""
            pointer.notify_absolute_motion(time(), {x}, {y});
            await sleep(80);
        """)

    async def wheel(self, target, notches):
        x, y = target["x"] + target["width"] / 2, target["y"] + target["height"] / 2
        await self.test.command("eval", code=POINTER + f"""
            pointer.notify_absolute_motion(time(), {x}, {y});
            await sleep(60);
            for (let i = 0; i < {notches}; i++) {{
                pointer.notify_discrete_scroll(time(), Clutter.ScrollDirection.DOWN,
                    Clutter.ScrollSource.WHEEL);
                await sleep(12);
            }}
            await sleep(120);
        """)

    async def set_clipboard(self, kind, text):
        await self.test.command("eval", code=f"""
            const St = imports.gi.St;
            St.Clipboard.get_default().set_text(St.ClipboardType.{kind}, {json.dumps(text)});
        """)

    async def use_theme(self, theme):
        self.daemon.config["theme"] = theme
        self.daemon.ConfigChanged("theme")
        await asyncio.sleep(0.5)

    async def begin_turn(self):
        self.daemon.Presence(True)
        self.daemon.set_state("thinking")
        await asyncio.sleep(SETTLE_S)

    async def end_turn(self):
        self.daemon.set_state("idle")
        self.daemon.Presence(False)
        await asyncio.sleep(0.6)

    async def take_down(self, request):
        self.daemon.ApprovalClosed(request)
        await asyncio.sleep(0.1)


async def steps_click(run, report):
    """Only a real click, and only on an armed button, answers."""
    daemon = run.daemon
    await run.begin_turn()
    report["before"] = await run.card()

    # Straight after it appears a click on Allow does nothing, and neither does one that was held
    # down from before it armed until after.
    daemon.Approval("normal", *SHORT, "click", 45)
    report["fresh"] = await run.card(0.05)
    await run.click(report["fresh"]["allow"])
    report["early_click"] = {"answers": run.answers(), "card": await run.card()}
    await run.shoot("normal_after_early_click")
    report["armed"] = await run.card(ARMED_S)
    await run.shoot("normal")

    # Keyboard: nothing on the card can be reached from it
    await run.test.command("keys", keyvals=[0xff0d])  # Return
    await run.test.command("keys", keyvals=[0x20])    # space
    await run.test.command("keys", keyvals=[0xff09])  # Tab
    await run.test.command("keys", keyvals=[0xff0d])
    report["after_keys"] = {"answers": run.answers(), "card": await run.card()}

    # The click that counts
    await run.click(report["armed"]["allow"])
    report["after_allow"] = {"answers": run.answers(), "card": await run.card()}
    daemon.ApprovalClosed("normal")  # the daemon's own close, after the card has gone

    daemon.Approval("held", *SHORT, "click", 45)
    fresh = await run.card(0.05)
    await run.press_and_hold(fresh["allow"], 1100)
    report["held_from_before_armed"] = {"answers": run.answers(), "card": await run.card()}
    await run.click(fresh["deny"])
    report["after_deny"] = {"answers": run.answers(), "card": await run.card()}

    # A right click on Allow is not a click on Allow
    daemon.Approval("right", *SHORT, "click", 45)
    armed = await run.card(ARMED_S)
    await run.test.command("click", x=armed["allow"]["x"] + 10, y=armed["allow"]["y"] + 10, button=3)
    report["right_click"] = {"answers": run.answers(), "card": await run.card()}
    # Nor is a click beside the buttons, and it must not reach what is under the card
    await run.test.command("click", x=armed["box"]["x"] + 6, y=armed["box"]["y"] + 6, button=1)
    report["click_on_card"] = {"answers": run.answers(), "card": await run.card()}
    await run.take_down("right")


async def steps_admin(run, report):
    daemon = run.daemon
    daemon.Approval("admin", "Run this as administrator?", "apt-get install -y gparted", "admin", 45)
    await asyncio.sleep(0.05)
    await run.shoot("admin_fresh")
    report["admin"] = await run.card(ARMED_S)
    await run.shoot("admin")
    await run.click(report["admin"]["allow"])
    report["after_admin"] = {"answers": run.answers(), "card": await run.card()}

    daemon.Approval("unknown-level", *SHORT, "root-please", 45)
    report["unknown_level"] = await run.card(0.2)
    await run.take_down("unknown-level")


async def steps_text(run, report):
    daemon = run.daemon
    daemon.Approval("long", *LONG, "click", 45)
    report["long"] = await run.card(ARMED_S)
    await run.shoot("long_top")
    await run.wheel(report["long"]["detailBox"], 12)
    report["long_some_way"] = await run.card()
    await run.wheel(report["long"]["detailBox"], 400)
    report["long_end"] = await run.card()
    await run.shoot("long_end")
    await run.take_down("long")

    daemon.Approval("long-admin", *LONG, "admin", 45)
    report["long_admin"] = await run.card(ARMED_S)
    await run.shoot("long_admin")
    await run.take_down("long-admin")

    daemon.Approval("unbroken", *UNBROKEN, "click", 45)
    report["unbroken"] = await run.card(ARMED_S)
    await run.wheel(report["unbroken"]["detailBox"], 400)
    report["unbroken_end"] = await run.card()
    await run.shoot("unbroken_end")
    await run.take_down("unbroken")

    daemon.Approval("markup", *MARKUP, "click", 45)
    report["markup"] = await run.card(0.4)
    report["markup_sent"] = list(MARKUP)
    await run.shoot("markup")
    await run.take_down("markup")

    daemon.Approval("no-detail", "Empty the Trash?", "", "click", 45)
    report["no_detail"] = await run.card(0.4)
    await run.shoot("no_detail")
    await run.take_down("no-detail")


async def steps_lifetime(run, report):
    daemon = run.daemon
    # The daemon closing it takes it down at once, and only that id does
    daemon.Approval("closes", *SHORT, "click", 45)
    await asyncio.sleep(0.3)
    daemon.ApprovalClosed("some-other-card")
    report["other_close"] = await run.card(0.1)
    daemon.ApprovalClosed("closes")
    report["closed"] = await run.card(0.03)

    # Runs out by itself, showing the time, and says nothing: no answer is the daemon's no
    before = len(run.answers())
    daemon.Approval("runs-out", *SHORT, "click", 3)
    report["time_start"] = (await run.card(0.1))["time"]
    report["time_later"] = (await run.card(1.3))["time"]
    report["time_almost"] = await run.card(1.2)
    report["time_over"] = await run.card(0.7)
    report["answers_on_timeout"] = run.answers()[before:]
    daemon.ApprovalClosed("runs-out")

    # A shell too busy to draw must not use up the wait: the card still has to be seen first
    daemon.Approval("stall", *SHORT, "click", 45)
    await run.test.command("eval", code="GLib.usleep(1500000);")
    report["after_stall"] = await run.card()
    report["after_stall_armed"] = await run.card(ARMED_S)
    await run.take_down("stall")

    # A new card that replaces an armed one starts unarmed, both after a close and without one
    for name, close_first in (("replaced_after_close", True), ("replaced_directly", False)):
        before = len(run.answers())
        daemon.Approval(f"{name}-1", *SHORT, "click", 45)
        old = await run.card(ARMED_S)
        if close_first:
            daemon.ApprovalClosed(f"{name}-1")
        daemon.Approval(f"{name}-2", *SHORT, "click", 45)
        fresh = await run.card(0.05)
        await run.click(old["allow"])
        early = run.answers()[before:]
        armed = await run.card(ARMED_S)
        await run.click(armed["allow"])
        report[name] = {"old": old, "fresh": fresh, "early": early, "armed": armed,
                        "answers": run.answers()[before:], "after": await run.card()}
        daemon.ApprovalClosed(f"{name}-2")
    await asyncio.sleep(0.1)


async def steps_placement(run, report):
    """Where it goes, and that it stays there."""
    daemon = run.daemon
    daemon.Approval("orb", *SHORT, "click", 45)
    report["with_orb"] = await run.card(0.3)
    await run.shoot("with_orb")
    await run.take_down("orb")

    await run.use_theme("band-quarter")
    daemon.Approval("band", *SHORT, "click", 45)
    report["with_band"] = await run.card(ARMED_S)
    await run.shoot("with_band")
    # Over a band, the click has to land on the card and not go through it
    await run.click(report["with_band"]["allow"])
    report["band_answers"] = run.answers()[-1:]
    await run.take_down("band")

    daemon.Approval("band-long", *LONG, "admin", 45)
    report["with_band_long"] = await run.card(0.3)
    await run.shoot("with_band_long")
    await run.take_down("band-long")

    await run.use_theme("band-full")
    daemon.Approval("full", *LONG, "click", 45)
    report["with_full_band"] = await run.card(ARMED_S)
    await run.shoot("with_full_band")
    await run.click(report["with_full_band"]["deny"])
    report["full_band_answers"] = run.answers()[-1:]
    await run.take_down("full")

    await run.use_theme("solid-orb")
    await run.end_turn()
    daemon.Approval("no-blob", *SHORT, "click", 45)
    report["no_blob"] = await run.card(0.3)
    await run.shoot("no_blob")
    await run.take_down("no-blob")

    # The card does not move under a pointer because the theme did
    await run.begin_turn()
    daemon.Approval("stays", *SHORT, "click", 45)
    before = await run.card(ARMED_S)
    await run.use_theme("band-quarter")
    report["theme_change"] = {"before": before, "after": await run.card()}
    await run.take_down("stays")
    await run.use_theme("solid-orb")

    # The monitors changing does move it, and then it has to be seen again before Allow works
    daemon.Approval("moves", *SHORT, "click", 45)
    await asyncio.sleep(ARMED_S)
    await run.test.command("eval", code="Main.layoutManager.emit('monitors-changed');")
    report["monitors_changed"] = await run.card(0.05)
    report["monitors_changed_armed"] = await run.card(ARMED_S)
    await run.take_down("moves")
    await run.end_turn()

    # In the overview it still takes a click
    await run.begin_turn()
    await run.test.command("eval", code="Main.overview.show();")
    await asyncio.sleep(1.2)
    daemon.Approval("overview", *SHORT, "click", 45)
    report["overview"] = await run.card(ARMED_S)
    await run.shoot("overview")
    await run.click(report["overview"]["allow"])
    report["overview_answers"] = run.answers()[-1:]
    await run.take_down("overview")
    await run.test.command("eval", code="Main.overview.hide();")
    await asyncio.sleep(1.0)
    await run.end_turn()


async def steps_caption(run, report):
    """The two never share the screen, and the caption goes on as before once the card has gone."""
    daemon = run.daemon
    await run.begin_turn()
    daemon.Transcript("delete the old reports", True)
    report["caption_before"] = await run.card(0.4)
    daemon.Approval("caption", *SHORT, "click", 45)
    report["caption_hidden"] = await run.card(0.4)
    await run.shoot("caption_hidden")
    daemon.Reply("One moment, I am asking first.")
    daemon.Error("This error waits for the card.")
    report["caption_held"] = await run.card(0.3)
    await run.click(report["caption_hidden"]["deny"])
    report["caption_after_card"] = await run.card(0.4)
    await run.shoot("caption_after_card")
    await asyncio.sleep(6.2)
    report["error_timer_ran"] = await run.card()

    # The normal end of a turn, with a card in it
    daemon.Approval("caption-2", *SHORT, "click", 45)
    await asyncio.sleep(0.3)
    daemon.ApprovalClosed("caption-2")
    await asyncio.sleep(0.1)
    daemon.set_state("speaking")
    daemon.Reply("Done, I moved them.")
    report["reply_after_card"] = await run.card(0.4)
    daemon.set_state("idle")
    report["reply_lingers"] = await run.card(0.6)
    await asyncio.sleep(3.2)
    report["reply_gone"] = await run.card()
    daemon.Presence(False)

    # A reply that came during the card shows after it, and then goes by itself
    await run.begin_turn()
    daemon.Approval("caption-3", *SHORT, "click", 45)
    await asyncio.sleep(0.2)
    daemon.Reply("Said while the card was up.")
    daemon.set_state("idle")
    await asyncio.sleep(0.2)
    daemon.ApprovalClosed("caption-3")
    report["held_reply"] = await run.card(0.4)
    await asyncio.sleep(4.2)
    report["held_reply_gone"] = await run.card()
    await run.end_turn()


async def steps_selection(run, report):
    daemon = run.daemon

    async def ask(request):
        daemon.SelectionWanted(request)
        await asyncio.sleep(0.3)
        return [c for c in daemon.calls if c[:2] == ["SelectionText", request]]

    report["selection_nothing"] = await ask("nothing")
    await run.set_clipboard("CLIPBOARD", f"clipboard {SECRET}")
    report["selection_clipboard"] = await ask("clipboard")
    await run.set_clipboard("PRIMARY", f"primary {SECRET}")
    report["selection_primary"] = await ask("primary")
    await run.set_clipboard("PRIMARY", "x" * 300000)
    report["selection_huge"] = [[c[0], c[1], len(c[2])] for c in await ask("huge")]


async def steps_window(run, report):
    """A real window beside a card: it keeps its focus and its input, so nothing has been grabbed."""
    daemon, test = run.daemon, run.test
    log = run.out / "window.log"
    log.touch()
    window = subprocess.Popen(
        [sys.executable, "-B", str(Path(__file__).with_name("card_window.py")), str(log)],
        env={**os.environ, "WAYLAND_DISPLAY": os.environ["TUX_WAYLAND_DISPLAY"],
             "GDK_BACKEND": "wayland", "GSK_RENDERER": "cairo"},
        stderr=subprocess.DEVNULL)

    async def focused():
        return await test.command("eval", code="return global.display.focus_window?.title ?? null;")

    try:
        for _ in range(100):
            if await focused() == "card-probe":
                break
            await asyncio.sleep(0.1)
        await asyncio.sleep(0.5)
        frame = await test.command("eval", code="""
            const {x, y, width, height} = global.display.focus_window.get_frame_rect();
            return {x, y, width, height};
        """)
        report["window_frame"] = frame
        await test.command("keys", keyvals=[0x61])  # a

        daemon.Approval("window", *SHORT, "click", 45)
        report["window_card"] = await run.card(ARMED_S)
        report["window_focus_card_up"] = await focused()
        await test.command("keys", keyvals=[0x62])  # b, with the card up
        await test.command("click", x=frame["x"] + 60, y=frame["y"] + 120, button=1)
        await asyncio.sleep(0.2)
        report["window_focus_after_window_click"] = await focused()
        report["window_card_still_up"] = (await run.card())["showing"]
        await run.shoot("window")
        await run.click(report["window_card"]["deny"])
        await asyncio.sleep(0.2)
        report["window_focus_after_card_click"] = await focused()
        await test.command("keys", keyvals=[0x63])  # c, after a click on the card
        await asyncio.sleep(0.3)
        report["window_input"] = log.read_text().split("\n")

        await test.command("eval", code="global.display.focus_window.make_fullscreen();")
        await asyncio.sleep(1.0)
        report["fullscreen"] = await test.command("eval", code="""
            const window = global.display.focus_window;
            return {fullscreen: window.is_fullscreen(),
                inFullscreen: Main.layoutManager.primaryMonitor.inFullscreen};
        """)
        daemon.Approval("fullscreen", *SHORT, "admin", 45)
        report["fullscreen_card"] = await run.card(ARMED_S)
        await run.shoot("fullscreen")
        await run.click(report["fullscreen_card"]["allow"])
        report["fullscreen_answers"] = run.answers()[-1:]
        await run.take_down("fullscreen")
    finally:
        window.terminate()
        window.wait(timeout=5)
    await asyncio.sleep(0.5)


async def steps_going_away(run, report):
    daemon = run.daemon
    report["counts_before"] = await run.test.command("counts")

    # Going offline takes the card down and says nothing, and coming back does not bring it back
    await run.begin_turn()
    before = len(run.answers())
    daemon.Approval("offline", *SHORT, "click", 45)
    report["before_offline"] = await run.card(0.3)
    await run.args.bus.release_name(BUS_NAME)
    report["offline"] = await run.card(0.5)
    await run.args.bus.request_name(BUS_NAME)
    report["back_online"] = await run.card(0.6)
    report["answers_offline"] = run.answers()[before:]

    # Disabling with a card up and its timers running leaves nothing behind
    await run.begin_turn()
    before = len(run.answers())
    daemon.Approval("disable", *SHORT, "click", 45)
    await asyncio.sleep(0.05)
    report["counts_card_up"] = await run.test.command("counts")
    await run.test.command("set_enabled", enabled=False)
    report["counts_disabled"] = await run.test.command("counts")
    daemon.Approval("nobody-home", *SHORT, "click", 45)
    daemon.ApprovalClosed("nobody-home")
    daemon.SelectionWanted("nobody-home")
    await asyncio.sleep(1.5)
    await run.test.command("set_enabled", enabled=True)
    report["reenabled"] = await run.card(SETTLE_S)
    report["counts_reenabled"] = await run.test.command("counts")
    report["answers_disable"] = run.answers()[before:]
    await run.end_turn()


STEPS = {"click": steps_click, "admin": steps_admin, "text": steps_text, "lifetime": steps_lifetime,
         "placement": steps_placement, "caption": steps_caption, "selection": steps_selection,
         "window": steps_window, "going_away": steps_going_away}


class Dictating:
    """Real windows in a real shell, each with a file that holds what its widget holds."""

    def __init__(self, run):
        self.run, self.daemon, self.test = run, run.daemon, run.test
        self.windows = []

    def open(self, kind, input_method=True):
        log = self.run.out / f"{kind}-{len(self.windows)}.txt"
        env = {**os.environ, "WAYLAND_DISPLAY": os.environ["TUX_WAYLAND_DISPLAY"],
               "GDK_BACKEND": "wayland", "GSK_RENDERER": "cairo"}
        if not input_method:
            env["GTK_IM_MODULE"] = "gtk-im-context-simple"  # as an app with no text-input support
        window = subprocess.Popen(
            [sys.executable, "-B", str(Path(__file__).with_name("dictate_window.py")), str(log), kind],
            env=env, stderr=subprocess.DEVNULL)
        self.windows.append(window)
        return log

    async def focus_on(self, kind):
        for _ in range(100):
            title = await self.test.command(
                "eval", code="return global.display.focus_window?.title ?? null;")
            if title == f"dictate-{kind}":
                await asyncio.sleep(0.5)
                return
            await asyncio.sleep(0.1)
        raise RuntimeError(f"dictate-{kind} never got the focus")

    async def layout(self, name, label):
        await self.test.command("eval", code=f"""
            const Meta = imports.gi.Meta;
            await global.backend.set_keymap_async(Meta.KeymapDescription.new_from_rules(
                'pc105', '{name}', '', '', ['{label}'], ['{name}']), 0, null);
        """)
        await asyncio.sleep(0.5)

    async def close_all(self):
        for window in self.windows:
            window.terminate()
            window.wait(timeout=5)
        self.windows = []
        await asyncio.sleep(0.5)

    async def go(self, request, words, settle=0.5):
        self.daemon.DictationStarted(request)
        await asyncio.sleep(0.1)
        self.daemon.TypeText(request, words, False)
        await asyncio.sleep(settle)
        return self.how(request)

    def how(self, request):
        return next((c[2] for c in self.daemon.calls if c[0] == "Typed" and c[1] == request), None)

    async def clipboard(self, kind="CLIPBOARD"):
        return await self.test.command("eval", code=f"""
            const St = imports.gi.St;
            return await new Promise(done => St.Clipboard.get_default().get_text(
                St.ClipboardType.{kind}, (_c, text) => done(text)));
        """)

    async def mimetypes(self):
        return await self.test.command("eval", code="""
            const St = imports.gi.St;
            return St.Clipboard.get_default().get_mimetypes(St.ClipboardType.CLIPBOARD);
        """)

    async def set_content(self, mimetype, byte_list):
        await self.test.command("eval", code=f"""
            const St = imports.gi.St;
            St.Clipboard.get_default().set_content(St.ClipboardType.CLIPBOARD, '{mimetype}',
                new GLib.Bytes(new Uint8Array({byte_list})));
        """)

    async def bytes_of(self, mimetype):
        return await self.test.command("eval", code=f"""
            const St = imports.gi.St;
            return await new Promise(done => St.Clipboard.get_default().get_content(
                St.ClipboardType.CLIPBOARD, '{mimetype}',
                (_c, bytes) => done(Array.from(bytes?.toArray() ?? []))));
        """)

    async def activate(self, kind):
        await self.test.command("eval", code=f"""
            global.get_window_actors().map(actor => actor.meta_window)
                .find(window => window.title === 'dictate-{kind}')
                .activate(global.get_current_time());
        """)

    async def note_on_screen(self):
        return await self.test.command("eval", code="""
            return Main.extensionManager.lookup('tuxistant@buengenio').stateObj._caption._reply.text;
        """)


async def dictate_text_field(d, report):
    log = d.open("text")
    await d.focus_on("text")
    await d.run.set_clipboard("CLIPBOARD", "keep-me")
    report["typed_how"] = await d.go("a", "Hello there, Привіт, ñandú 你好! ")
    report["typed_text"] = log.read_text()
    report["typed_clipboard"] = await d.clipboard()

    report["enter_how"] = await d.go("b", "first\nsecond\r\x1b[Athird\u2028fourth\u202efifth ")
    report["enter_text"] = log.read_text()

    d.daemon.TypeText("stranger", "INJECTED ", False)
    await asyncio.sleep(0.3)
    d.daemon.TypeText("b", "REPLAYED ", False)
    await asyncio.sleep(0.3)
    report["unexpected_text"] = log.read_text()
    report["unexpected_how"] = [d.how("stranger")]

    d.daemon.DictationStarted("c")
    await asyncio.sleep(0.1)
    await d.test.command("eval", code="Main.overview.show();")
    await asyncio.sleep(1.0)
    d.daemon.TypeText("c", "over a shell dialog ", False)
    await asyncio.sleep(0.5)
    report["modal_how"] = d.how("c")
    report["modal_clipboard"] = await d.clipboard()
    await d.test.command("eval", code="Main.overview.hide();")
    await asyncio.sleep(1.0)
    report["modal_text"] = log.read_text()

    d.daemon.DictationStarted("d")
    await asyncio.sleep(0.1)
    other = d.open("button")
    await d.focus_on("button")
    d.daemon.TypeText("d", "for the first window ", False)
    await asyncio.sleep(0.5)
    report["moved_how"] = d.how("d")
    report["moved_clipboard"] = await d.clipboard()
    report["moved_note"] = await d.note_on_screen()
    report["moved_first_window"] = log.read_text()
    report["moved_other_window"] = other.read_text()
    await d.close_all()


async def dictate_text_field_more(d, report):
    log = d.open("text")
    await d.focus_on("text")
    window = d.windows[-1]

    report["joiners_how"] = await d.go("j1", "می\u200cخواهم 👩\u200d💻 ")
    report["joiners_text"] = log.read_text()

    d.daemon.DictationStarted("o1")
    d.daemon.DictationStarted("o2")
    await asyncio.sleep(0.1)
    d.daemon.TypeText("o1", "one ", False)
    while d.how("o1") is None:      # the real daemon waits for this too
        await asyncio.sleep(0.01)
    d.daemon.TypeText("o2", "two ", False)
    await asyncio.sleep(0.5)
    report["overlap_how"] = [d.how("o1"), d.how("o2")]
    report["overlap_text"] = log.read_text()

    await d.run.set_clipboard("CLIPBOARD", "keep-me")
    d.daemon.DictationStarted("t1")
    await asyncio.sleep(0.1)
    d.daemon.TypeText("t1", "what the room said ", True)
    await asyncio.sleep(0.5)
    report["ran_out_how"] = d.how("t1")
    report["ran_out_clipboard"] = await d.clipboard()
    report["ran_out_note"] = await d.note_on_screen()
    report["ran_out_window"] = log.read_text()

    await d.run.set_clipboard("CLIPBOARD", "keep-me")
    before = len(log.read_text())
    report["long_how"] = await d.go("l1", "word " * 1000, settle=2.6)
    report["long_alive"] = window.poll() is None
    report["long_added"] = len(log.read_text()) - before
    report["long_clipboard"] = await d.clipboard()
    report["cyrillic_long_how"] = await d.go("l2", "слово " * 400, settle=2.6)
    report["cyrillic_long_alive"] = window.poll() is None
    await d.close_all()


async def dictate_password(d, report):
    log = d.open("password")
    await d.focus_on("password")
    await d.run.set_clipboard("CLIPBOARD", "keep-me")
    report["password_how"] = await d.go("e", "hunter2 ")
    report["password_text"] = log.read_text()
    report["password_clipboard"] = await d.clipboard()
    report["password_note"] = await d.note_on_screen()

    d.daemon.DictationStarted("e2")
    await asyncio.sleep(0.1)
    await d.test.command("eval", code="Main.overview.show();")
    await asyncio.sleep(1.0)
    d.daemon.TypeText("e2", "hunter3 ", False)
    await asyncio.sleep(0.5)
    report["password_modal_how"] = d.how("e2")
    report["password_modal_clipboard"] = await d.clipboard()
    await d.test.command("eval", code="Main.overview.hide();")
    await asyncio.sleep(1.0)
    await d.close_all()


async def dictate_paste(d, report):
    log = d.open("text", input_method=False)
    await d.focus_on("text")
    await d.run.set_clipboard("CLIPBOARD", "before-clipboard")
    await d.run.set_clipboard("PRIMARY", "before-primary")
    report["paste_how"] = await d.go("f", "Pasted words, ñandú Привіт ", settle=2.6)
    report["paste_text"] = log.read_text()
    report["paste_clipboard"] = await d.clipboard()
    report["paste_primary"] = await d.clipboard("PRIMARY")

    await d.layout("ua", "Ukrainian")
    report["cyrillic_how"] = await d.go("g", "on a Cyrillic layout ", settle=0.6)
    report["cyrillic_text"] = log.read_text()
    await d.run.set_clipboard("CLIPBOARD", "CTRLV ")
    await d.test.command("eval", code=POINTER + """
        const keys = global.stage.context.get_backend().get_default_seat()
            .create_virtual_device(Clutter.InputDeviceType.KEYBOARD_DEVICE);
        for (const [key, state] of [[Clutter.KEY_Control_L, 1], [Clutter.KEY_v, 1],
            [Clutter.KEY_v, 0], [Clutter.KEY_Control_L, 0]]) {
            keys.notify_keyval(time(), key, state);
            await sleep(20);
        }
    """)
    await asyncio.sleep(0.5)
    report["cyrillic_ctrl_v_text"] = log.read_text()
    await d.layout("us", "English")

    await asyncio.sleep(2.0)    # the last paste has finished with the clipboard
    d.daemon.DictationStarted("i")
    await asyncio.sleep(0.1)
    d.daemon.TypeText("i", "words on the way ", False)
    await asyncio.sleep(0.4)
    await d.run.set_clipboard("CLIPBOARD", "copied-meanwhile")
    await asyncio.sleep(2.2)
    report["meanwhile_clipboard"] = await d.clipboard()

    await d.set_content("image/png", [137, 80, 78, 71, 13, 10, 26, 10])
    report["image_how"] = await d.go("im", "beside a picture ", settle=2.6)
    report["image_mimetypes"] = await d.mimetypes()
    report["image_bytes"] = await d.bytes_of("image/png")

    await d.set_content("x-kde-passwordManagerHint", [115, 101, 99, 114, 101, 116])
    report["hint_how"] = await d.go("pm", "after a password manager ", settle=2.6)
    report["hint_clipboard"] = await d.clipboard()
    report["hint_mimetypes"] = await d.mimetypes()

    await d.run.set_clipboard("CLIPBOARD", "first-clipboard")
    first = await d.go("b1", "first burst ", settle=0.3)
    second = await d.go("b2", "second burst ", settle=2.6)
    report["bursts_how"] = [first, second]
    report["bursts_clipboard"] = await d.clipboard()
    await d.close_all()


async def dictate_slow_apps(d, report):
    log = d.open("slow", input_method=False)
    await d.focus_on("slow")
    await d.run.set_clipboard("CLIPBOARD", "my-bank-password")
    await d.run.set_clipboard("PRIMARY", "my-bank-password")
    report["slow_how"] = await d.go("sl", "the dictated words ", settle=3.0)
    report["slow_text"] = log.read_text()
    await d.close_all()

    owner = d.open("owner")
    await d.focus_on("owner")
    await asyncio.sleep(1.0)
    log = d.open("text", input_method=False)
    await d.focus_on("text")
    Path(str(owner) + ".hang").touch()
    await asyncio.sleep(0.3)
    d.daemon.DictationStarted("h1")
    await asyncio.sleep(0.1)
    began = time.monotonic()
    d.daemon.TypeText("h1", "for this window ", False)
    while d.how("h1") is None and time.monotonic() - began < 3:
        await asyncio.sleep(0.02)
    report["hung_seconds"] = time.monotonic() - began
    report["hung_how"] = d.how("h1")
    await asyncio.sleep(0.4)
    report["hung_text"] = log.read_text()
    await asyncio.sleep(2.2)
    report["hung_clipboard_after"] = await d.clipboard()
    await d.close_all()
    await asyncio.sleep(6.0)    # the hung app has to wake up to close


async def dictate_focus_moves_during_the_save(d, report):
    other = d.open("button", input_method=False)
    await d.focus_on("button")
    owner = d.open("owner")
    await d.focus_on("owner")
    await asyncio.sleep(1.0)
    log = d.open("text", input_method=False)
    await d.focus_on("text")
    Path(str(owner) + ".hang").touch()
    await asyncio.sleep(0.3)
    d.daemon.DictationStarted("m1")
    await asyncio.sleep(0.1)
    d.daemon.TypeText("m1", "for the window I was in ", False)
    await d.activate("button")
    await asyncio.sleep(1.0)
    report["save_moved_how"] = d.how("m1")
    report["save_moved_text"] = log.read_text()
    report["save_moved_other"] = other.read_text()
    report["save_moved_clipboard"] = await d.clipboard()
    await d.close_all()
    await asyncio.sleep(6.0)


async def dictate_disabled_during_the_save(d, report):
    owner = d.open("owner")
    await d.focus_on("owner")
    await asyncio.sleep(1.0)
    d.open("text", input_method=False)
    await d.focus_on("text")
    await asyncio.sleep(0.2)
    Path(str(owner) + ".hang").touch()
    await asyncio.sleep(0.3)
    d.daemon.DictationStarted("x1")
    await asyncio.sleep(0.1)
    d.daemon.TypeText("x1", "private words ", False)
    await d.test.command("eval", code="Main.extensionManager.disableExtension('tuxistant@buengenio');")
    await asyncio.sleep(9.0)
    report["disabled_clipboard"] = await d.clipboard()
    report["disabled_primary"] = await d.clipboard("PRIMARY")
    await d.close_all()


async def dictate_nothing_to_type_in(d, report):
    log = d.open("button", input_method=False)
    await d.focus_on("button")
    await d.run.set_clipboard("CLIPBOARD", "keep-me")
    report["button_how"] = await d.go("j", "no field here ", settle=2.6)
    report["button_text"] = log.read_text()
    report["button_clipboard"] = await d.clipboard()
    await d.close_all()
    report["no_window_how"] = await d.go("k", "no window at all ")
    report["no_window_clipboard"] = await d.clipboard()


async def scenario_dictation(daemon, test, args):
    report = {}
    d = Dictating(Run(daemon, test, args))
    try:
        for step in (dictate_text_field, dictate_text_field_more, dictate_password, dictate_paste,
                     dictate_nothing_to_type_in, dictate_slow_apps,
                     dictate_focus_moves_during_the_save, dictate_disabled_during_the_save):
            await step(d, report)
    finally:
        for window in d.windows:
            window.kill()
        report["typed_calls"] = [c for c in daemon.calls if c[0] == "Typed"]
        (d.run.out / "report.json").write_text(json.dumps(report, indent=1))


async def scenario_verify(daemon, test, args):
    run = Run(daemon, test, args)
    report = {}
    try:
        for step in STEPS.values():
            await step(run, report)
    finally:
        report["calls"] = [c for c in daemon.calls if c[0] in ("Answer", "SelectionText", "Cancel")]
        report["calls"] = [c if c[0] != "SelectionText" else c[:2] + [len(c[2])] for c in report["calls"]]
        (run.out / "report.json").write_text(json.dumps(report, indent=1))


async def scenario_fit(daemon, test, args):
    """Every kind of blob on whatever monitor this run has, for a short and a long request."""
    run = Run(daemon, test, args)
    report = {}
    try:
        for theme in ("solid-orb", "band-quarter", "band-half", "band-full", "orb-top-left",
                      "orb-center", "orb-huge", None):
            if theme:
                await run.use_theme(theme)
                await run.begin_turn()
            else:
                await run.end_turn()
            name = theme or "no-blob"
            for kind, level, text in (("short", "click", SHORT), ("long", "admin", LONG),
                                      ("unbroken", "click", UNBROKEN)):
                daemon.Approval(f"{name}-{kind}", *text, level, 45)
                report[f"{name}/{kind}"] = await run.card(0.25)
                await run.shoot(f"{name}-{kind}")
                await run.take_down(f"{name}-{kind}")
        title = "Replace every file in " + "a-very-long-folder-name/" * 7 + "?"
        await run.end_turn()
        daemon.Approval("long-title", title[:200], LONG_DETAIL, "admin", 45)
        report["longest-title/long"] = await run.card(0.25)
        await run.shoot("longest-title")
        await run.take_down("long-title")
    finally:
        (run.out / "report.json").write_text(json.dumps(report, indent=1))


async def scenario_monitors(daemon, test, args):
    """Two monitors: the card goes where the blob is, or else to where the pointer is."""
    run = Run(daemon, test, args)
    report = {}
    try:
        first = await test.command("eval", code="""
            return Main.layoutManager.monitors.map(m => ({x: m.x, y: m.y, width: m.width, height: m.height}));
        """)
        report["monitors"] = first
        second = first[1]
        await run.move_to(second["x"] + 100, second["y"] + 100)
        daemon.Approval("no-blob-second", *SHORT, "click", 45)
        report["no_blob_second"] = await run.card(0.3)
        await run.shoot("no_blob_second")
        await run.take_down("no-blob-second")

        await run.begin_turn()
        await run.move_to(first[0]["x"] + 100, first[0]["y"] + 100)
        daemon.Approval("blob-second", *LONG, "click", 45)
        report["blob_second_pointer_first"] = await run.card(0.3)
        await run.take_down("blob-second")
        await run.end_turn()

        daemon.Approval("no-blob-first", *SHORT, "click", 45)
        report["no_blob_first"] = await run.card(0.3)
        await run.take_down("no-blob-first")
    finally:
        (run.out / "report.json").write_text(json.dumps(report, indent=1))


async def scenario_real(daemon, test, args):
    """The production service and controller on this bus, so what is tested is the contract itself
    and not this file's idea of it. Only the audio factories are missing, and no turn runs."""
    run = Run(daemon, test, args)
    ctl = daemon.controller
    report = {}

    async def ask(title, detail, level="click"):
        task = asyncio.create_task(ctl.ask(Ask(title, detail, dict, level)))
        await asyncio.sleep(0.05)
        return task

    try:
        # A turn in progress with no blob yet: Presence has to get there ahead of Approval
        ctl.set_state("thinking")
        await asyncio.sleep(0.5)
        title = "Delete \u202egnp.exe\u202c <b>now</b>?"
        detail = "rm -rf /\x1b[2K\u200b & <i>" + "/home/user/project/file " * 200 + TAIL
        first = await ask(title, detail)
        report["hostile"] = await run.card(0.3)
        report["hostile_sent"] = [printable(title, MAX_TITLE_CHARS), printable(detail, MAX_DETAIL_CHARS)]
        await run.shoot("real_hostile")
        await run.wheel(report["hostile"]["detailBox"], 400)
        await run.shoot("real_hostile_end")
        armed = await run.card(ARMED_S)
        await run.click(armed["allow"])
        report["allow_result"] = await asyncio.wait_for(first, 3)
        report["after_allow"] = await run.card(0.1)

        # The next action asks at once: the click meant for the last card must not count
        second = await ask("Next one?", "second action")
        fresh = await run.card()
        report["next_fresh"] = fresh
        await run.click(fresh["allow"])
        report["next_waits"] = not second.done()
        armed = await run.card(ARMED_S)
        await run.click(armed["deny"])
        report["deny_result"] = await asyncio.wait_for(second, 3)

        admin = await ask("Run this as administrator?", "apt-get upgrade", "admin")
        report["admin"] = await run.card(ARMED_S)
        await run.click(report["admin"]["allow"])
        report["admin_result"] = await asyncio.wait_for(admin, 3)

        # The turn being cancelled closes the card, and that is a no
        third = await ask("Cancelled?", "third action")
        await asyncio.sleep(0.2)
        third.cancel()
        report["cancelled"] = await run.card(0.1)

        real_controller.APPROVAL_WAIT_S = 2
        fourth = await ask("Runs out?", "fourth action")
        report["short_card"] = await run.card(0.2)
        report["timeout_result"] = await asyncio.wait_for(fourth, 4)
        report["timed_out"] = await run.card(0.1)
        report["presence_after"] = ctl.presence

        await run.set_clipboard("CLIPBOARD", f"clipboard {SECRET}")
        report["selection_clipboard"] = await ctl.selection()
        await run.set_clipboard("PRIMARY", f"primary {SECRET}")
        report["selection_primary"] = await ctl.selection()
    finally:
        (run.out / "report.json").write_text(json.dumps(report, indent=1))


class LoudMic:
    """Stands in for the microphone: a steady tone, loud enough to count as a voice."""

    async def start(self):
        pass

    async def stop(self):
        pass

    async def read(self):
        await asyncio.sleep(0.04)
        return array.array("h", [12000, -12000] * 480).tobytes()


SHORTCUT = POINTER + """
const keys = driver._keys ??= Clutter.get_default_backend().get_default_seat()
    .create_virtual_device(Clutter.InputDeviceType.KEYBOARD_DEVICE);
const held = [Clutter.KEY_Control_L, Clutter.KEY_Super_L, Clutter.KEY_d];
for (const key of held) {
    keys.notify_keyval(time(), key, Clutter.KeyState.PRESSED);
    await sleep(30);
}
for (const key of held.reverse()) {
    keys.notify_keyval(time(), key, Clutter.KeyState.RELEASED);
    await sleep(30);
}
"""


async def scenario_dictation_real(daemon, test, args):
    """The production service and controller against the real extension. Only the microphone and
    the speech to text are stand-ins, so the guard, Hello and Typed are the real thing."""
    d = Dictating(Run(daemon, test, args))
    ctl = daemon.controller
    ctl.make_mic = LoudMic
    errors = []
    send = ctl.emit
    ctl.emit = lambda name, *a: (errors.append(a[0]) if name == "Error" else None, send(name, *a))[1]
    said = iter(["Real words, Привіт. ", "From the shortcut. ", "Over the overview. "])

    async def hears(_config, _pcm):
        return next(said)

    async def finished():
        for _ in range(100):
            if not ctl.active and not ctl.deliveries:
                return
            await asyncio.sleep(0.05)

    report = {}
    real_controller.transcribe = hears
    try:
        for _ in range(100):
            if ctl.typist:
                break
            await asyncio.sleep(0.05)
        report["typist"] = ctl.typist
        log = d.open("text")
        await d.focus_on("text")
        await ctl.dictate_start()
        await asyncio.sleep(0.7)
        await ctl.dictate_end()
        await finished()
        await asyncio.sleep(0.3)
        report["held_text"] = log.read_text()

        await test.command("eval", code=SHORTCUT)
        await asyncio.sleep(0.7)
        report["shortcut_started"] = ctl.dictation is not None
        await test.command("eval", code=SHORTCUT)
        await finished()
        await asyncio.sleep(0.3)
        report["shortcut_text"] = log.read_text()

        await test.command("eval", code=SHORTCUT)
        await asyncio.sleep(0.7)
        report["overview_listening"] = ctl.dictation is not None
        await test.command("eval", code="Main.overview.show();")
        await asyncio.sleep(1.0)
        await test.command("eval", code=SHORTCUT)
        await finished()
        report["overview_stopped"] = not ctl.active
        await test.command("eval", code="Main.overview.hide();")
        await asyncio.sleep(1.0)
        report["errors"] = errors
        report["state"] = ctl.state

        await ctl.dictate_start()
        await asyncio.sleep(0.3)
        await test.command("eval", code="Main.extensionManager.disableExtension('tuxistant@buengenio');")
        await finished()
        report["disabled_stopped"] = not ctl.active
        report["disabled_typist"] = ctl.typist
    finally:
        for window in d.windows:
            window.kill()
        (d.run.out / "report.json").write_text(json.dumps(report, indent=1))


def real_daemon(args):
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    config = Config(out / "config.json")
    config.set("theme", args.theme)
    return service.Daemon(config, History(out / "history.db"), Skills(),
                          mic_factory=None, speaker_factory=None)


SCENARIOS = {"dictation": scenario_dictation, "dictation_real": scenario_dictation_real,
             "verify": scenario_verify, "fit": scenario_fit, "monitors": scenario_monitors,
             "real": scenario_real}


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--theme", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--scenario", default="verify")
    parser.add_argument("--ready-timeout", type=float, default=90)
    args = parser.parse_args()

    bus = await MessageBus().connect()
    test = Test()
    bus.export(BUS_PATH, test)
    if args.scenario in ("real", "dictation_real"):
        daemon = real_daemon(args)
        await service.serve(bus, daemon)
    else:
        daemon = Daemon(args.theme)
        bus.export(BUS_PATH, daemon)
        await bus.request_name(BUS_NAME)
    print("ready for the shell", flush=True)
    args.bus = bus
    await asyncio.wait_for(test.ready.wait(), args.ready_timeout)
    await SCENARIOS[args.scenario](daemon, test, args)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except (TimeoutError, RuntimeError, AssertionError) as e:
        print(f"harness failure: {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(1)
