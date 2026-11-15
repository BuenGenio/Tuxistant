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
from tuxistant.dictation import NOTHING_HEARD, TOO_SHORT
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
    def DictationMode(self, id, mode) -> "ss":
        return [id, mode]

    @dbus_signal()
    def TypeText(self, id, text, only_copy) -> "ssb":
        return [id, text, only_copy]

    @dbus_signal()
    def FollowUp(self, question, hint, seconds) -> "ssi":
        return [question, hint, seconds]


class Run:
    """What the scenarios do over and over."""

    def __init__(self, daemon, test, args):
        self.daemon, self.test, self.args = daemon, test, args
        self.out = Path(args.out)

    async def card(self, settle=0.0):
        if settle:
            await asyncio.sleep(settle)
        return await self.test.command("eval", code=SNAPSHOT)

    @staticmethod
    def laid_out(card, card_id):
        """Showing as card_id, with its Allow button placed, so a click can find it."""
        allow = card.get("allow") or {}
        return card.get("id") == card_id and isinstance(allow.get("x"), (int, float)) and (
            allow.get("width") or 0) > 0

    async def shown(self, card_id, settle=0.0):
        """The card card_id, read settle seconds after it is showing and placed, so a busy
        machine that is slow to put it up doesn't make the read too early."""
        await self.wait_card(lambda card: self.laid_out(card, card_id))
        return await self.card(settle)

    async def wait_card(self, test, deadline_s=10.0):
        """The card once test holds for it, read as often as the shell answers, so a busy
        machine waits longer rather than reading too soon. The last read, past the deadline."""
        end = time.monotonic() + deadline_s
        while True:
            card = await self.card()
            if test(card) or time.monotonic() > end:
                return card
            await asyncio.sleep(0.02)

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
    report["fresh"] = await run.wait_card(lambda card: run.laid_out(card, "normal"))
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
    fresh = await run.shown("held", 0.05)
    await run.press_and_hold(fresh["allow"], 1100)
    report["held_from_before_armed"] = {"answers": run.answers(), "card": await run.card()}
    await run.click(fresh["deny"])
    report["after_deny"] = {"answers": run.answers(), "card": await run.card()}

    # A right click on Allow is not a click on Allow
    daemon.Approval("right", *SHORT, "click", 45)
    armed = await run.shown("right", ARMED_S)
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
    report["unknown_level"] = await run.shown("unknown-level", 0.2)
    await run.take_down("unknown-level")


async def steps_text(run, report):
    daemon = run.daemon
    daemon.Approval("long", *LONG, "click", 45)
    report["long"] = await run.shown("long", ARMED_S)
    await run.shoot("long_top")
    await run.wheel(report["long"]["detailBox"], 12)
    report["long_some_way"] = await run.card()
    await run.wheel(report["long"]["detailBox"], 400)
    report["long_end"] = await run.card()
    await run.shoot("long_end")
    await run.take_down("long")

    daemon.Approval("long-admin", *LONG, "admin", 45)
    report["long_admin"] = await run.shown("long-admin", ARMED_S)
    await run.shoot("long_admin")
    await run.take_down("long-admin")

    daemon.Approval("unbroken", *UNBROKEN, "click", 45)
    report["unbroken"] = await run.shown("unbroken", ARMED_S)
    await run.wheel(report["unbroken"]["detailBox"], 400)
    report["unbroken_end"] = await run.card()
    await run.shoot("unbroken_end")
    await run.take_down("unbroken")

    daemon.Approval("markup", *MARKUP, "click", 45)
    report["markup"] = await run.shown("markup", 0.4)
    report["markup_sent"] = list(MARKUP)
    await run.shoot("markup")
    await run.take_down("markup")

    daemon.Approval("no-detail", "Empty the Trash?", "", "click", 45)
    report["no_detail"] = await run.shown("no-detail", 0.4)
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
    runs_out = lambda card: card.get("id") == "runs-out"   # noqa: E731
    start = await run.wait_card(runs_out)
    report["time_start"] = start.get("time")
    report["time_later"] = (await run.wait_card(
        lambda card: runs_out(card) and card.get("time") != start.get("time"))).get("time")
    report["time_almost"] = await run.wait_card(
        lambda card: runs_out(card) and card.get("time") == "Denied in 1 s")
    report["time_over"] = await run.wait_card(lambda card: not card["showing"])
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
        old = await run.shown(f"{name}-1", ARMED_S)
        if close_first:
            daemon.ApprovalClosed(f"{name}-1")
        daemon.Approval(f"{name}-2", *SHORT, "click", 45)
        fresh = await run.shown(f"{name}-2", 0.05)
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
    report["with_orb"] = await run.shown("orb", 0.3)
    await run.shoot("with_orb")
    await run.take_down("orb")

    await run.use_theme("band-quarter")
    daemon.Approval("band", *SHORT, "click", 45)
    report["with_band"] = await run.shown("band", ARMED_S)
    await run.shoot("with_band")
    # Over a band, the click has to land on the card and not go through it
    await run.click(report["with_band"]["allow"])
    report["band_answers"] = run.answers()[-1:]
    await run.take_down("band")

    daemon.Approval("band-long", *LONG, "admin", 45)
    report["with_band_long"] = await run.shown("band-long", 0.3)
    await run.shoot("with_band_long")
    await run.take_down("band-long")

    await run.use_theme("band-full")
    daemon.Approval("full", *LONG, "click", 45)
    report["with_full_band"] = await run.shown("full", ARMED_S)
    await run.shoot("with_full_band")
    await run.click(report["with_full_band"]["deny"])
    report["full_band_answers"] = run.answers()[-1:]
    await run.take_down("full")

    await run.use_theme("solid-orb")
    await run.end_turn()
    daemon.Approval("no-blob", *SHORT, "click", 45)
    report["no_blob"] = await run.shown("no-blob", 0.3)
    await run.shoot("no_blob")
    await run.take_down("no-blob")

    # The card does not move under a pointer because the theme did
    await run.begin_turn()
    daemon.Approval("stays", *SHORT, "click", 45)
    before = await run.shown("stays", ARMED_S)
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
    report["overview"] = await run.shown("overview", ARMED_S)
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
    report["caption_hidden"] = await run.shown("caption", 0.4)
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
        report["window_card"] = await run.shown("window", ARMED_S)
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
        report["fullscreen_card"] = await run.shown("fullscreen", ARMED_S)
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
    report["before_offline"] = await run.shown("offline", 0.3)
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

    async def heard_on_screen(self):
        return await self.test.command("eval", code="""
            return Main.extensionManager.lookup('tuxistant@buengenio').stateObj._caption._heard.text;
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


async def dictate_modes_of_an_old_go(d, report):
    """A mode for a go that isn't the latest changes nothing on screen."""
    d.daemon.StateChanged("listening")
    d.daemon.DictationStarted("m-old")
    d.daemon.DictationStarted("m-new")
    await asyncio.sleep(0.3)
    d.daemon.DictationMode("m-old", "tap")
    await asyncio.sleep(0.3)
    report["old_mode_caption"] = await d.heard_on_screen()
    d.daemon.DictationMode("m-new", "tap")
    await asyncio.sleep(0.3)
    report["new_mode_caption"] = await d.heard_on_screen()
    d.daemon.DictationMode("m-new", "nonsense")
    await asyncio.sleep(0.3)
    report["unknown_mode_caption"] = await d.heard_on_screen()
    d.daemon.StateChanged("idle")
    await asyncio.sleep(0.3)


async def dictate_hint_outlives_an_earlier_go(d, report):
    """Go A's words were still at the server when go B began hands-free: what A brings, words or
    a note, must not take B's stop hint off the screen."""
    d.open("text")
    await d.focus_on("text")

    async def caption():
        return await d.test.command("eval", code="""
            const caption = Main.extensionManager.lookup('tuxistant@buengenio').stateObj._caption;
            return [caption.actor.visible, caption._heard.text, caption._reply.text];
        """)

    d.daemon.StateChanged("listening")
    d.daemon.DictationStarted("h-a")
    await asyncio.sleep(0.1)
    d.daemon.StateChanged("idle")
    await asyncio.sleep(0.1)
    d.daemon.StateChanged("listening")
    d.daemon.DictationStarted("h-b")
    d.daemon.DictationMode("h-b", "tap")
    await asyncio.sleep(0.3)
    d.daemon.TypeText("h-a", "earlier words ", False)
    await asyncio.sleep(0.5)
    report["carry_typed_how"] = d.how("h-a")
    report["carry_after_words"] = await caption()
    d.daemon.Error(NOTHING_HEARD)
    await asyncio.sleep(0.3)
    report["carry_with_note"] = await caption()
    await asyncio.sleep(6.5)    # the caption's ERROR_MS
    report["carry_after_note"] = await caption()
    d.daemon.StateChanged("idle")
    await asyncio.sleep(0.3)

    # A go's own "too short" says nothing about stopping: it has stopped.
    d.daemon.StateChanged("listening")
    d.daemon.DictationStarted("h-c")
    d.daemon.DictationMode("h-c", "tap")
    await asyncio.sleep(0.3)
    d.daemon.Error(TOO_SHORT)
    d.daemon.StateChanged("idle")
    await asyncio.sleep(0.3)
    report["carry_own_short"] = await caption()
    await asyncio.sleep(6.5)
    report["carry_own_short_after"] = await caption()
    await d.close_all()


FOLLOW_UP_QUESTION = "I found two reports. Which one should I print?"
CAPTION_LOOK = r"""
const caption = Main.extensionManager.lookup('tuxistant@buengenio').stateObj._caption;
const shown = label => label?.visible ? label.text : '';
return [caption.actor.visible && caption.actor.opacity > 0, shown(caption._heard),
        shown(caption._reply), shown(caption._hint), shown(caption._note)];
"""


async def dictate_stop_line_goes_with_the_go(d, report):
    """Once a hands-free go stops listening, nothing may still say to tap Fn to stop it: a tap
    then does nothing, and two start a new go."""
    async def look():
        return await d.test.command("eval", code=CAPTION_LOOK)

    d.daemon.StateChanged("listening")
    d.daemon.DictationStarted("s-a")
    d.daemon.DictationMode("s-a", "tap")
    await asyncio.sleep(0.3)
    report["stop_line_listening"] = await look()
    d.daemon.Presence(False)
    d.daemon.StateChanged("idle")
    await asyncio.sleep(0.4)
    report["stop_line_stopped"] = await look()
    await asyncio.sleep(2.0)
    report["stop_line_later"] = await look()


async def dictate_question_outlives_an_earlier_go(d, report):
    """A hold ended on a question while a go's words were still at the server: what that go
    brings, words or a note, must not take the question and its hint off the screen."""
    async def look():
        return await d.test.command("eval", code=CAPTION_LOOK)

    d.open("text")
    await d.focus_on("text")
    d.daemon.StateChanged("listening")
    d.daemon.DictationStarted("q-a")
    await asyncio.sleep(0.1)
    d.daemon.StateChanged("idle")
    await asyncio.sleep(0.3)
    d.daemon.FollowUp(FOLLOW_UP_QUESTION, "Hold Fn to answer", 10)
    await asyncio.sleep(0.3)
    report["late_question"] = await look()
    d.daemon.TypeText("q-a", "late words ", False)
    await asyncio.sleep(0.5)
    report["late_typed_how"] = d.how("q-a")
    report["late_after_words"] = await look()
    d.daemon.Error(NOTHING_HEARD)
    await asyncio.sleep(0.3)
    report["late_with_note"] = await look()
    d.daemon.FollowUp("", "", 0)    # the question goes: the note it carried stays its own time
    await asyncio.sleep(0.4)
    report["late_note_after_question"] = await look()
    await asyncio.sleep(6.5)
    report["late_note_gone"] = await look()
    await d.close_all()


async def scenario_dictation(daemon, test, args):
    report = {}
    d = Dictating(Run(daemon, test, args))
    try:
        for step in (dictate_text_field, dictate_text_field_more, dictate_password, dictate_paste,
                     dictate_nothing_to_type_in, dictate_slow_apps,
                     dictate_focus_moves_during_the_save, dictate_modes_of_an_old_go,
                     dictate_hint_outlives_an_earlier_go, dictate_stop_line_goes_with_the_go,
                     dictate_question_outlives_an_earlier_go,
                     dictate_disabled_during_the_save):
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
                report[f"{name}/{kind}"] = await run.shown(f"{name}-{kind}", 0.25)
                await run.shoot(f"{name}-{kind}")
                await run.take_down(f"{name}-{kind}")
        title = "Replace every file in " + "a-very-long-folder-name/" * 7 + "?"
        await run.end_turn()
        daemon.Approval("long-title", title[:200], LONG_DETAIL, "admin", 45)
        report["longest-title/long"] = await run.shown("long-title", 0.25)
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
        report["no_blob_second"] = await run.shown("no-blob-second", 0.3)
        await run.shoot("no_blob_second")
        await run.take_down("no-blob-second")

        await run.begin_turn()
        await run.move_to(first[0]["x"] + 100, first[0]["y"] + 100)
        daemon.Approval("blob-second", *LONG, "click", 45)
        report["blob_second_pointer_first"] = await run.shown("blob-second", 0.3)
        await run.take_down("blob-second")
        await run.end_turn()

        daemon.Approval("no-blob-first", *SHORT, "click", 45)
        report["no_blob_first"] = await run.shown("no-blob-first", 0.3)
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


async def dictate_with_fn(d, ctl, finished, errors, log, report):
    """Fn as the helper reports it, through the daemon's own wiring of it, to the real extension."""
    fn = service.fn_client(ctl.config, ctl, path="unused")

    async def keys(*words, gap=0.05):
        for word in words:
            await fn.handle(word)
            await asyncio.sleep(gap)

    await keys("down", "up", "down", "up")      # tap, tap: hands-free
    await asyncio.sleep(0.8)
    report["free_listening"] = ctl.dictating
    report["free_caption"] = await d.heard_on_screen()
    await keys("down", "up")
    await finished()
    await asyncio.sleep(0.3)
    report["free_text"] = log.read_text()

    await keys("down", "up", "down")            # tap, then hold: push-to-talk
    await asyncio.sleep(0.6)
    report["hold_caption"] = await d.heard_on_screen()
    await keys("up")
    await finished()
    await asyncio.sleep(0.3)
    report["hold_text"] = log.read_text()

    await keys("down", "up", "down", "up", "down", "up", gap=0.02)   # stopped as soon as it began
    await finished()
    await asyncio.sleep(0.3)
    report["short_note"] = await d.note_on_screen()
    report["short_text"] = log.read_text()
    report["short_error"] = errors.count(TOO_SHORT)
    if TOO_SHORT in errors:
        errors.remove(TOO_SHORT)


async def scenario_dictation_real(daemon, test, args):
    """The production service and controller against the real extension. Only the microphone and
    the speech to text are stand-ins, so the guard, Hello and Typed are the real thing."""
    d = Dictating(Run(daemon, test, args))
    ctl = daemon.controller
    ctl.make_mic = LoudMic
    errors = []
    send = ctl.emit
    ctl.emit = lambda name, *a: (errors.append(a[0]) if name == "Error" else None, send(name, *a))[1]
    said = iter(["Real words, Привіт. ", "From the shortcut. ", "Over the overview. ",
                 "Hands free. ", "Held down. "])

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
        report["shortcut_caption"] = await d.heard_on_screen()
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
        await dictate_with_fn(d, ctl, finished, errors, log, report)
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


FOLLOW_UP_LOOK = r"""
const caption = ext._caption;
return {
    state: ext._state,
    blob: ext._blob.visible,
    caption: caption.actor.visible && caption.actor.opacity > 0,
    reply: caption._reply.visible ? caption._reply.text : '',
    hint: caption._hint.visible ? caption._hint.text : '',
    heard: caption._heard.visible ? caption._heard.text : '',
};
"""


class QuietSpeaker:
    """Stands in for the speaker: takes what it is given and plays nothing."""

    def __init__(self, on_level):
        self.on_level = on_level

    async def start(self):
        pass

    def write(self, _pcm):
        self.on_level(0.4)

    async def drain(self):
        await asyncio.sleep(0.2)
        self.on_level(0.0)

    async def stop(self):
        self.on_level(0.0)


async def scenario_followup_real(daemon, test, args):
    """The production service and controller against the real extension, with a stand-in server
    on loopback: a question after the blob was let go stays on screen with how to answer it, and
    the next hold carries on the same connection. Only the microphone, the speaker, speech to
    text and the server are stand-ins."""
    from tests.test_daemon_fakes import FakeServer, tone

    ctl = daemon.controller
    ctl.make_mic, ctl.make_speaker = LoudMic, QuietSpeaker
    question = "I found two reports. Which one should I print?"
    replies = [question, question, "It is noon.", question, question]
    heard = iter(["print my report", "print my other report", "what time is it",
                  "print the last one", "print the very last one", "print my report again"])

    async def script(conn):
        for count, text in enumerate(replies, 1):
            await conn.wait_for(lambda: conn.types().count("response.create") >= count, 60)
            await conn.reply(text, pcm=tone(0.2))

    async def hears(_config, _pcm):
        return next(heard)

    async def never_a_fact(_heard, _config):
        return False

    async def says(_config, _text, _voice):
        return tone(0.2)

    async def look():
        return await test.command("eval", code=FOLLOW_UP_LOOK)

    async def hold(release=True):
        await ctl.hold_start()
        await asyncio.sleep(0.6)
        if release:
            await ctl.hold_end()
            for _ in range(200):
                if not ctl.active:
                    return
                await asyncio.sleep(0.05)

    server = FakeServer(script)
    await server.__aenter__()
    daemon.config.values.update(server=f"127.0.0.1:{server.port}", api_key="good")
    real_controller.transcribe = hears
    real_controller.knowledge.is_fact = never_a_fact
    real_controller.speech = says
    report = {}
    try:
        for _ in range(100):
            if ctl.face:
                break
            await asyncio.sleep(0.05)
        await hold()
        report["after_release_presence"] = ctl.presence
        await asyncio.sleep(1.0)
        report["question_up"] = await look()
        await test.command("shoot", path=str(Path(args.out) / "followup_question.png"))
        daemon.Presence(False)  # a stray one must not cut the question's time short
        await asyncio.sleep(5.0)
        report["question_still_up"] = await look()
        await asyncio.sleep(5.5)
        report["question_gone"] = await look()

        await hold()
        await asyncio.sleep(1.0)
        report["second_question_up"] = await look()
        await hold(release=False)
        report["answering"] = await look()
        await ctl.hold_end()
        for _ in range(200):
            if not ctl.active:
                break
            await asyncio.sleep(0.05)
        await asyncio.sleep(1.0)
        report["plain_reply"] = await look()

        await hold()
        await asyncio.sleep(1.0)
        report["third_question_up"] = await look()
        # A go over the question: its hint takes the question's place, and the talk carries on.
        go = await ctl.dictate_start("tap")
        await asyncio.sleep(0.6)
        report["dictating_over_question"] = await look()
        await ctl.drop_go(go)   # as a Fn shortcut on the press that started it does
        await asyncio.sleep(0.6)
        await hold()
        await asyncio.sleep(1.0)
        report["fourth_question_up"] = await look()
        # Fn tapped, then Fn with another key: a Fn shortcut, never dictation, so the question
        # the go's listening hid comes back.
        go = await ctl.dictate_start()
        await asyncio.sleep(0.2)
        report["second_press_over_question"] = await look()
        await ctl.drop_go(go)
        await asyncio.sleep(0.6)
        report["question_back"] = await look()
        await ctl.cancel()
        await asyncio.sleep(0.6)
        report["cancelled"] = await look()
        report["connections"] = len(server.connections)
        report["asked"] = [e["item"]["content"][0]["text"] for e in server.connections[0].log
                           if e["type"] == "conversation.item.create"]

        # A timer's words take the place of a question, and the question is gone after them.
        await hold()
        await asyncio.sleep(1.0)
        report["fifth_question_up"] = await look()
        await (await ctl.say_when_free("Your tea is ready."))
        report["timer_over_question"] = await look()
        report["timer_left_question"] = ctl.talk.asked
        await asyncio.sleep(4.0)    # its words linger about 3 s, as any reply's do
        report["timer_gone"] = await look()
        report["state"] = ctl.state
    finally:
        await server.__aexit__()
        (Path(args.out) / "report.json").write_text(json.dumps(report, indent=1))


def real_daemon(args):
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    config = Config(out / "config.json")
    config.set("theme", args.theme)
    return service.Daemon(config, History(out / "history.db"), Skills(),
                          mic_factory=None, speaker_factory=None)


SCENARIOS = {"dictation": scenario_dictation, "dictation_real": scenario_dictation_real,
             "verify": scenario_verify, "fit": scenario_fit, "monitors": scenario_monitors,
             "real": scenario_real, "followup_real": scenario_followup_real}


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
    if args.scenario in ("real", "dictation_real", "followup_real"):
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
