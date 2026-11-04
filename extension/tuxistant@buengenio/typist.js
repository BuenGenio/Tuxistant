// Puts dictated words where the cursor is. The daemon has the words, but only the shell can see
// the focused window and field, so where they may go is decided here. See "Dictation" in
// docs/CONTRACT.md.
import Clutter from 'gi://Clutter';
import GLib from 'gi://GLib';
import St from 'gi://St';

import * as Main from 'resource:///org/gnome/shell/ui/main.js';

import {readClipboard} from './approval.js';

const SELECTIONS = [St.ClipboardType.CLIPBOARD, St.ClipboardType.PRIMARY];
const MAX_COMMIT_BYTES = 3500;  // a Wayland message holds 4096, and going over kills the app
const SAVE_WAIT_MS = 300;       // whoever holds the clipboard may be hung, and the words can't wait
const PASTE_WAIT_MS = 2000;     // an app reads the clipboard after the keys, in its own time
const MAX_EXPECTED = 8;
const PASSWORD_HINT = 'x-kde-passwordManagerHint';
const HIDDEN = Clutter.InputContentHintFlags.HIDDEN_TEXT |
    Clutter.InputContentHintFlags.SENSITIVE_DATA;

// Whoever sent this, it can't press Enter. The two joiners are spelling, in Persian and in emoji.
const plain = text => text.replace(/(?![‌‍])\p{Cf}/gu, '')
    .replace(/[\p{Cc}\p{Zl}\p{Zp}]/gu, ' ');

const fitsOneMessage = text => new TextEncoder().encode(text).length <= MAX_COMMIT_BYTES;

const after = (ms, value) => new Promise(resolve => {
    GLib.timeout_add(GLib.PRIORITY_DEFAULT, ms, () => {
        resolve(value);
        return GLib.SOURCE_REMOVE;
    });
});

function isSecretField() {
    const {currentFocus, contentPurpose, contentHints} = Main.inputMethod;
    return Boolean(currentFocus) &&
        (contentPurpose === Clutter.InputContentPurpose.PASSWORD || (contentHints & HIDDEN) !== 0);
}

function canReach(window) {
    return Boolean(window) && window === global.display.focus_window &&
        Main.modalCount === 0 && !Main.sessionMode.isLocked;
}

// What to put back after a paste. Nothing, for what a password manager copied: it clears its own
// copies, and one held here would stay.
async function save(type) {
    const clipboard = St.Clipboard.get_default();
    const mimetypes = clipboard.get_mimetypes(type);
    const mimetype = mimetypes.find(name => name.startsWith('text/plain')) ?? mimetypes[0];
    if (!mimetype || mimetypes.includes(PASSWORD_HINT))
        return null;
    // A copy, made inside the callback: GJS doesn't keep what a callback is given, and holding on
    // to it crashes the whole shell when it is used later.
    const bytes = await new Promise(resolve => {
        clipboard.get_content(type, mimetype, (_clipboard, content) => {
            resolve(content ? new GLib.Bytes(content.toArray().slice()) : null);
        });
    });
    return bytes ? {mimetype, bytes} : null;
}

export class Typist {
    constructor() {
        const seat = global.stage.context.get_backend().get_default_seat();
        this._keyboard = seat.create_virtual_device(Clutter.InputDeviceType.KEYBOARD_DEVICE);
        this._expected = new Map();  // id: where that go started
        this._saved = null;
        this._pasted = null;
        this._timer = 0;
    }

    expect(id) {
        this._expected.set(id, {window: global.display.focus_window, secret: isSecretField()});
        if (this._expected.size > MAX_EXPECTED)
            this._expected.delete(this._expected.keys().next().value);
    }

    // Resolves with what `Typed` reports, or null for words nobody here was expecting.
    async type(id, words, onlyCopy) {
        const target = this._expected.get(id);
        if (!target)
            return null;
        this._expected.delete(id);
        if (target.secret || isSecretField())
            return 'refused';
        const text = plain(words);
        if (onlyCopy || !canReach(target.window))
            return this._copy(text);
        if (Main.inputMethod.currentFocus && fitsOneMessage(text)) {
            Main.inputMethod.commit(text);
            return 'typed';
        }
        return this._paste(text, target.window);
    }

    _copy(text) {
        St.Clipboard.get_default().set_text(St.ClipboardType.CLIPBOARD, text);
        return 'copied';
    }

    async _paste(text, window) {
        if (this._timer)  // what the last paste saved is still the user's
            GLib.source_remove(this._timer);
        this._timer = 0;
        this._saved ??= await Promise.race([Promise.all(SELECTIONS.map(save)),
            after(SAVE_WAIT_MS, null)]);
        if (!this._keyboard)
            return null;
        if (!canReach(window)) {
            this._saved = null;
            return this._copy(text);
        }
        const clipboard = St.Clipboard.get_default();
        SELECTIONS.forEach(type => clipboard.set_text(type, text));
        this._pasted = text;
        this._keys([Clutter.KEY_Shift_L, Clutter.KEY_Insert]);
        this._timer = GLib.timeout_add(GLib.PRIORITY_DEFAULT, PASTE_WAIT_MS, () => {
            this._timer = 0;
            this._putBack();
            return GLib.SOURCE_REMOVE;
        });
        return 'pasted';
    }

    _putBack() {
        const [saved, pasted] = [this._saved, this._pasted];
        this._saved = this._pasted = null;
        if (pasted === null)
            return;
        const clipboard = St.Clipboard.get_default();
        SELECTIONS.forEach(async (type, i) => {
            // Anything else there is the user's own copy, or the lock screen having cleared it.
            if (await readClipboard(type) !== pasted)
                return;
            if (saved?.[i])
                clipboard.set_content(type, saved[i].mimetype, saved[i].bytes);
            else
                clipboard.set_text(type, '');
        });
    }

    _keys(keyvals) {
        const send = (keyval, state) => this._keyboard.notify_keyval(
            Clutter.get_current_event_time() * 1000, keyval, state);
        keyvals.forEach(keyval => send(keyval, Clutter.KeyState.PRESSED));
        keyvals.reverse().forEach(keyval => send(keyval, Clutter.KeyState.RELEASED));
    }

    destroy() {
        if (this._timer)
            GLib.source_remove(this._timer);
        this._timer = 0;
        this._putBack();
        this._expected.clear();
        this._keyboard = null;
    }
}
