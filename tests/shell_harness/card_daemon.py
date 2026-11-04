#!/usr/bin/env python3
"""The approval half of the daemon's bus API, for card.sh.

Sends the real Approval, ApprovalClosed and SelectionWanted signals on the isolated bus and records
what comes back, so an Answer in the report is one that a click really sent. Pointer and keyboard
events are real too, through virtual devices in that shell. Nothing is judged here: the scenarios
write report.json and PNGs, and tests/test_approval_card.py looks at them.
"""
import argparse
import asyncio
import json
import os
import subprocess
import sys
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

    @dbus_signal()
    def SelectionWanted(self, id) -> "s":
        return id


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


def real_daemon(args):
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    config = Config(out / "config.json")
    config.set("theme", args.theme)
    return service.Daemon(config, History(out / "history.db"), Skills(),
                          mic_factory=None, speaker_factory=None)


SCENARIOS = {"verify": scenario_verify, "fit": scenario_fit, "monitors": scenario_monitors,
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
    if args.scenario == "real":
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
