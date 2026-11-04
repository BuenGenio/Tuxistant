// The caption: a small glass card near the blob with what was heard and what Tux said.
// Nothing here is reactive or focusable, so it can never take focus or clicks from a window.
import Clutter from 'gi://Clutter';
import GLib from 'gi://GLib';
import Pango from 'gi://Pango';
import St from 'gi://St';

import * as Main from 'resource:///org/gnome/shell/ui/main.js';

const MAX_WIDTH = 460;
const MAX_CHARS = 320;
const GAP = 14;
const EDGE = 12;
const FADE_MS = 160;
const ERROR_MS = 6000;

// Long enough to read: a base plus a little per character.
const lingerMs = text => Math.min(8000, 2500 + 40 * text.length);

export class Caption {
    constructor() {
        this.actor = new St.BoxLayout({
            style_class: 'tuxistant-caption',
            orientation: Clutter.Orientation.VERTICAL,
            reactive: false,
            can_focus: false,
            visible: false,
            opacity: 0,
        });
        this._heard = this._addLabel('tuxistant-caption-heard');
        this._reply = this._addLabel('tuxistant-caption-reply');
        Main.layoutManager.addTopChrome(this.actor);
        this._timeoutId = 0;
        this._rect = null;
        this._monitor = null;
        this._anchor = 'bottom-right';
        this._blobVisible = false;
        this._isError = false;
    }

    _addLabel(styleClass) {
        const label = new St.Label({style_class: styleClass, visible: false});
        label.clutter_text.set({
            line_wrap: true,
            line_wrap_mode: Pango.WrapMode.WORD_CHAR,
            ellipsize: Pango.EllipsizeMode.NONE,
        });
        this.actor.add_child(label);
        return label;
    }

    get showing() {
        return this.actor.visible && this.actor.opacity > 0;
    }

    follow(rect, anchor, monitor, blobVisible) {
        this._rect = rect;
        this._anchor = anchor;
        this._monitor = monitor;
        this._blobVisible = blobVisible;
        if (this.actor.visible)
            this._place();
    }

    showHeard(text) {
        if (this._isError || this._reply.visible)
            this._clearLabels();
        this._set(this._heard, text.slice(-MAX_CHARS));
        this._present();
    }

    showReply(text) {
        if (this._isError)
            this._clearLabels();
        this._set(this._reply, text.length > MAX_CHARS ? `${text.slice(0, MAX_CHARS)}…` : text);
        this._present();
    }

    showError(text) {
        this._clearLabels();
        this._isError = true;
        this._reply.add_style_class_name('tuxistant-caption-error');
        this._set(this._reply, text);
        this._present();
        this._hideIn(ERROR_MS);
    }

    // The turn is over: keep the text up for a moment. An error keeps its own, longer timer.
    linger() {
        if (this.actor.visible && !this._isError)
            this._hideIn(lingerMs(this._reply.text));
    }

    // A new turn started: nothing from the last one should stay around.
    reset() {
        if (!this._isError)
            this.hide();
    }

    hide() {
        this._cancelTimeout();
        if (!this.actor.visible)
            return;
        this.actor.ease({
            opacity: 0,
            duration: FADE_MS,
            mode: Clutter.AnimationMode.EASE_OUT_QUAD,
            onStopped: finished => {
                if (finished) {
                    this.actor.hide();
                    this._clearLabels();
                }
            },
        });
    }

    _set(label, text) {
        label.text = text;
        label.visible = text !== '';
    }

    _clearLabels() {
        this._isError = false;
        this._reply.remove_style_class_name('tuxistant-caption-error');
        this._set(this._heard, '');
        this._set(this._reply, '');
    }

    // Text that comes with no blob on screen goes where the user is, not where the blob last was.
    // What is already showing stays put, wherever the pointer has gone.
    _toCurrentMonitor() {
        const layoutManager = Main.layoutManager;
        const current = layoutManager.currentMonitor ?? layoutManager.primaryMonitor;
        if (!this._blobVisible && !this.actor.visible && current?.index !== this._monitor?.index) {
            this._monitor = current;
            this._rect = null;
        }
    }

    _present() {
        this._cancelTimeout();
        this._toCurrentMonitor();
        this.actor.show();
        this._place();
        this.actor.ease({
            opacity: 255,
            duration: FADE_MS,
            mode: Clutter.AnimationMode.EASE_OUT_QUAD,
        });
    }

    _hideIn(ms) {
        this._cancelTimeout();
        this._timeoutId = GLib.timeout_add(GLib.PRIORITY_DEFAULT, ms, () => {
            this._timeoutId = 0;
            this.hide();
            return GLib.SOURCE_REMOVE;
        });
    }

    _cancelTimeout() {
        if (this._timeoutId) {
            GLib.source_remove(this._timeoutId);
            this._timeoutId = 0;
        }
    }

    _place() {
        const layoutManager = Main.layoutManager;
        const monitor = this._monitor ?? layoutManager.currentMonitor ?? layoutManager.primaryMonitor;
        if (!monitor)
            return;
        const area = layoutManager.getWorkAreaForMonitor(monitor.index);
        const rect = this._rect ?? {
            x: area.x + area.width - EDGE, y: area.y + area.height - EDGE, width: 0, height: 0,
        };
        const [vertical, horizontal = 'center'] = this._anchor.split('-');

        // A fixed size would be what the preferred size reports back, so measure without it.
        this.actor.set_size(-1, -1);
        const maxWidth = Math.min(MAX_WIDTH, monitor.width - 2 * EDGE);
        const [, naturalWidth] = this.actor.get_preferred_width(-1);
        const width = Math.min(naturalWidth, maxWidth);
        const [, height] = this.actor.get_preferred_height(width);
        this.actor.set_size(width, height);

        let x = rect.x + (rect.width - width) / 2;
        if (horizontal === 'left')
            x = rect.x;
        else if (horizontal === 'right')
            x = rect.x + rect.width - width;

        let y = rect.y - height - GAP;
        if (vertical !== 'bottom' || y < monitor.y + EDGE)
            y = rect.y + rect.height + GAP;
        if (y + height > monitor.y + monitor.height - EDGE)
            y = rect.y + rect.height - height - GAP;  // a band as tall as the screen

        this.actor.set_position(
            Math.round(Math.min(Math.max(x, monitor.x + EDGE), monitor.x + monitor.width - width - EDGE)),
            Math.round(Math.max(y, monitor.y + EDGE)));
    }

    destroy() {
        this._cancelTimeout();
        this.actor.remove_all_transitions();
        this.actor.destroy();
        this.actor = null;
    }
}
