// The approval card: where a person says yes to something that matters. Only a click answers it,
// so it takes no keyboard focus and grabs nothing, and a card that goes unanswered is a no: the
// daemon's own timeout says so. Every card is built from nothing, so that no timer, hover or armed
// button of the last one can carry over to the next.
import Clutter from 'gi://Clutter';
import GLib from 'gi://GLib';
import Pango from 'gi://Pango';
import St from 'gi://St';

import * as Main from 'resource:///org/gnome/shell/ui/main.js';

const MAX_WIDTH = 480;
const MAX_HEIGHT_SHARE = 0.7;  // of the work area: a long command scrolls instead
const MIN_ROOM = 240;          // for a card with a long title and a long detail to work
const MIN_TIME_WIDTH = 400;    // narrower, and the buttons need the room of the time left
const GAP = 14;
const EDGE = 12;
const FADE_MS = 120;
const ARM_MS = 600;
const MAX_SELECTION_CHARS = 100000;  // a selection can be any size, and a bus message cannot

function readClipboard(type) {
    return new Promise(resolve => {
        St.Clipboard.get_default().get_text(type, (_clipboard, text) => resolve(text ?? ''));
    });
}

// What is selected on screen, or else what is on the clipboard. Private: it must never be logged.
export async function selectedText() {
    const text = await readClipboard(St.ClipboardType.PRIMARY) ||
        await readClipboard(St.ClipboardType.CLIPBOARD);
    return text.slice(0, MAX_SELECTION_CHARS);
}

// Next to the blob the way the caption is, but always inside the work area, wherever the blob is:
// a band is as wide as the screen and can be as tall.
function position(rect, anchor, area, width, height) {
    const [vertical, horizontal = 'center'] = anchor.split('-');
    const top = area.y + EDGE;
    const bottom = area.y + area.height - EDGE;

    let x = rect.x + (rect.width - width) / 2;
    if (horizontal === 'left')
        x = rect.x;
    else if (horizontal === 'right')
        x = rect.x + rect.width - width;

    const above = rect.y - height - GAP;
    const below = rect.y + rect.height + GAP;
    let y = vertical === 'bottom' ? above : below;
    if (y < top)
        y = below;
    if (y + height > bottom)
        y = above;

    return [
        Math.round(Math.min(Math.max(x, area.x + EDGE), area.x + area.width - width - EDGE)),
        Math.round(Math.min(Math.max(y, top), bottom - height)),
    ];
}

// As tall as the text wants, short of covering the blob. With too little room beside it, as with
// a tall band, the card goes over the blob, and then what is left of the screen is what counts.
function maxHeight(rect, area) {
    const share = Math.max(Math.floor(area.height * MAX_HEIGHT_SHARE),
        Math.min(MIN_ROOM, area.height - 2 * EDGE));
    if (!rect)
        return share;
    const free = Math.max(rect.y - area.y, area.y + area.height - rect.y - rect.height) - GAP - EDGE;
    return free < MIN_ROOM ? share : Math.min(free, share);
}

class Card {
    constructor({id, title, detail, level, seconds}, onAnswer, onExpired) {
        this.id = id;
        this._onAnswer = onAnswer;
        this._onExpired = onExpired;
        this._armId = 0;
        this._tickId = 0;
        const admin = level !== 'click';  // something unknown gets the stronger warning

        this.actor = new St.BoxLayout({
            style_class: 'tuxistant-approval',
            orientation: Clutter.Orientation.VERTICAL,
            reactive: true,
            can_focus: false,
            opacity: 0,
        });
        if (admin)
            this.actor.add_style_class_name('tuxistant-approval-admin');
        this.actor.connect('destroy', () => this._stopTimers());

        this._title = this._label('tuxistant-approval-title', title);
        this.actor.add_child(this._title);
        this._addDetail(detail);
        this.actor.add_child(this._footer(admin));

        // Above everything so far, the caption included: nothing it does can cover the card.
        Main.layoutManager.addTopChrome(this.actor);
        // A fullscreen window that bypasses the compositor would leave the card on no screen at all.
        global.compositor.disable_unredirect();

        this._deadline = GLib.get_monotonic_time() + seconds * GLib.USEC_PER_SEC;
        this._showTime(seconds);
        this._tickId = GLib.timeout_add(GLib.PRIORITY_DEFAULT, 1000, () => this._tick());
    }

    // Plain text: this can quote anything, and markup in it must show as what it is.
    _label(styleClass, text) {
        const label = new St.Label({style_class: styleClass, text});
        label.clutter_text.set({
            use_markup: false,
            line_wrap: true,
            line_wrap_mode: Pango.WrapMode.WORD_CHAR,
            ellipsize: Pango.EllipsizeMode.NONE,
        });
        return label;
    }

    _addDetail(detail) {
        this._content = new St.BoxLayout({orientation: Clutter.Orientation.VERTICAL});
        this._detail = this._label('tuxistant-approval-detail', detail);
        this._detail.visible = detail !== '';
        this._content.add_child(this._detail);
        this._scroll = new St.ScrollView({
            style_class: 'tuxistant-approval-scroll',
            hscrollbar_policy: St.PolicyType.NEVER,
            vscrollbar_policy: St.PolicyType.AUTOMATIC,
            overlay_scrollbars: false,
            x_expand: true,
            y_expand: true,
            visible: detail !== '',
            child: this._content,
        });
        this.actor.add_child(this._scroll);

        // The tail of a command is where the surprise would be, so a scrollbar alone is too quiet.
        this._more = new St.Label({
            style_class: 'tuxistant-approval-more',
            text: 'There is more: scroll to read all of it',
            visible: false,
        });
        this.actor.add_child(this._more);
        const adjustment = this._scroll.vadjustment;
        adjustment.connect('notify::value', () => {
            const atEnd = adjustment.value + adjustment.page_size >= adjustment.upper - 1;
            this._more.opacity = atEnd ? 0 : 255;  // not hidden: the text must not move as it scrolls
        });
    }

    _footer(admin) {
        const footer = new St.BoxLayout({style_class: 'tuxistant-approval-footer'});
        this._time = new St.Label({
            style_class: 'tuxistant-approval-time',
            y_align: Clutter.ActorAlign.CENTER,
        });
        footer.add_child(this._time);
        this._deny = this._button('Deny', 'tuxistant-approval-deny', false);
        this._deny.set({x_expand: true, x_align: Clutter.ActorAlign.END});
        footer.add_child(this._deny);
        this._allow = this._button(admin ? 'Allow as administrator' : 'Allow',
            'tuxistant-approval-allow', true);
        this._allow.reactive = false;
        footer.add_child(this._allow);
        return footer;
    }

    _button(label, styleClass, allow) {
        const button = new St.Button({
            label,
            style_class: `tuxistant-approval-button ${styleClass}`,
            can_focus: false,
        });
        button.connect('clicked', () => this._onAnswer(allow));
        return button;
    }

    _showTime(seconds) {
        this._time.text = `Denied in ${seconds} s`;
    }

    _tick() {
        const left = Math.ceil((this._deadline - GLib.get_monotonic_time()) / GLib.USEC_PER_SEC);
        if (left <= 0) {
            this._tickId = 0;
            this._onExpired();
            return GLib.SOURCE_REMOVE;
        }
        this._showTime(left);
        return GLib.SOURCE_CONTINUE;
    }

    // Allow only works once the card has been in view for a moment, so a click on its way to
    // whatever was here before cannot approve it. The wait starts with the card on screen, not with
    // this call: a busy shell draws late, then runs every timer that is due at once.
    _arm() {
        this._allow.reactive = false;
        this._cancelArm();
        this.actor.ease({
            opacity: 255,
            duration: FADE_MS,
            mode: Clutter.AnimationMode.EASE_OUT_QUAD,
            onComplete: () => {
                this._armId = GLib.timeout_add(GLib.PRIORITY_DEFAULT, ARM_MS, () => {
                    this._armId = 0;
                    this._allow.reactive = true;
                    return GLib.SOURCE_REMOVE;
                });
            },
        });
    }

    _cancelArm() {
        if (this._armId)
            GLib.source_remove(this._armId);
        this._armId = 0;
    }

    _stopTimers() {
        this._cancelArm();
        if (this._tickId)
            GLib.source_remove(this._tickId);
        this._tickId = 0;
    }

    // Nothing may push the buttons off the card, so on a screen this short the title gives up its
    // fixed place and scrolls with the detail.
    _letTitleScroll() {
        this._title.get_parent().remove_child(this._title);
        this._content.insert_child_at_index(this._title, 0);
        this._scroll.show();
    }

    // Whenever it lands somewhere new it has to be seen there again before Allow works.
    place({rect, anchor, monitor, visible}) {
        const layoutManager = Main.layoutManager;
        const screen = visible && monitor
            ? monitor : layoutManager.currentMonitor ?? layoutManager.primaryMonitor;
        if (!screen)
            return;
        const area = layoutManager.getWorkAreaForMonitor(screen.index);
        const blob = visible ? rect : null;
        const width = Math.min(MAX_WIDTH, area.width - 2 * EDGE);
        const room = maxHeight(blob, area);
        this._time.visible = width >= MIN_TIME_WIDTH;
        if (room < MIN_ROOM)
            this._letTitleScroll();

        // A fixed size would be what the preferred size reports back, so measure without it.
        this._more.hide();
        this.actor.set_size(-1, -1);
        const [, natural] = this.actor.get_preferred_height(width);
        this._more.visible = natural > room;
        const height = Math.min(natural, room);
        this.actor.set_size(width, height);

        const corner = {
            x: area.x + area.width - EDGE, y: area.y + area.height - EDGE, width: 0, height: 0,
        };
        const [x, y] = blob
            ? position(blob, anchor, area, width, height)
            : position(corner, 'bottom-right', area, width, height);
        this.actor.set_position(x, y);
        this._arm();
    }

    destroy() {
        global.compositor.enable_unredirect();
        this.actor.destroy();
    }
}

export class Approval {
    // onAnswer(id, allow) is a click. onGone fires when a card has come down and left none up.
    constructor({onAnswer, onGone}) {
        this._onAnswer = onAnswer;
        this._onGone = onGone;
        this._blob = {rect: null, anchor: 'bottom-right', monitor: null, visible: false};
        this._card = null;
        Main.layoutManager.connectObject('monitors-changed',
            () => this._card?.place(this._blob), this);
    }

    get showing() {
        return this._card !== null;
    }

    // Noted for the next card only: one that is up stays where it is, as Allow must not slide
    // under the pointer because a theme changed.
    follow(rect, anchor, monitor, visible) {
        this._blob = {rect, anchor, monitor, visible};
    }

    show(id, title, detail, level, seconds) {
        this._card?.destroy();
        const card = new Card({id, title, detail, level, seconds}, allow => {
            this._onAnswer(id, allow);
            this.close(id);
        }, () => this.close(id));
        this._card = card;
        card.place(this._blob);
    }

    // Takes down that card, or whichever is up. A close that comes late leaves the next card alone.
    close(id = this._card?.id) {
        if (!this._card || this._card.id !== id)
            return;
        this._card.destroy();
        this._card = null;
        this._onGone();
    }

    destroy() {
        Main.layoutManager.disconnectObject(this);
        this._card?.destroy();
        this._card = null;
    }
}
