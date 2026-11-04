// Test only. Never installed outside the harness. Takes commands from the fake daemon over the
// private session bus, runs them inside the shell and answers with CommandDone.
import Clutter from 'gi://Clutter';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import Shell from 'gi://Shell';
import St from 'gi://St';

import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';

const BUS_NAME = 'org.tuxistant.Daemon';
const BUS_PATH = '/org/tuxistant/Daemon';
const IFACE = 'org.tuxistant.Test';
const TARGET = 'tuxistant@buengenio';
const ACTIVE = 1;

const sleep = ms => new Promise(resolve => GLib.timeout_add(GLib.PRIORITY_DEFAULT, ms, () => {
    resolve();
    return GLib.SOURCE_REMOVE;
}));
const items = menu => menu._getMenuItems().filter(i => i.label).map(i => i.label.text);
const box = actor => {
    const [x, y] = actor.get_position();
    const [width, height] = actor.get_size();
    return {x, y, width, height};
};

export default class ShotExtension extends Extension {
    enable() {
        this._subscription = Gio.DBus.session.signal_subscribe(
            null, IFACE, 'Command', BUS_PATH, null, Gio.DBusSignalFlags.NONE,
            (_c, sender, _p, _i, _n, params) => {
                const [id, json] = params.deepUnpack();
                this._peer = sender;
                this._run(id, JSON.parse(json));
            });
        this._poll = GLib.timeout_add(GLib.PRIORITY_DEFAULT, 100, () => {
            if (Main.layoutManager._startingUp ||
                Main.extensionManager.lookup(TARGET)?.state !== ACTIVE)
                return GLib.SOURCE_CONTINUE;
            this._poll = 0;
            Main.overview.hide();
            // Startup banners, such as the one about screen lock needing GDM, would cover shots.
            Main.messageTray.bannerBlocked = true;
            Main.messageTray.getSources().forEach(source => source.destroy());
            this._reply('Ready', null);
            return GLib.SOURCE_REMOVE;
        });
    }

    disable() {
        if (this._poll)
            GLib.source_remove(this._poll);
        Gio.DBus.session.signal_unsubscribe(this._subscription);
    }

    _reply(method, params) {
        // By unique name: the scenario may have given up the well-known one to act offline.
        Gio.DBus.session.call(this._peer ?? BUS_NAME, BUS_PATH, IFACE, method, params, null,
            Gio.DBusCallFlags.NONE, -1, null, null);
    }

    async _run(id, command) {
        let result;
        try {
            result = await this[`_op_${command.op}`](command) ?? null;
        } catch (e) {
            result = {error: `${e}\n${e.stack}`};
        }
        this._reply('CommandDone', new GLib.Variant('(ss)', [id, JSON.stringify(result)]));
    }

    // Free-form JS for experiments: the body of an async function that sees imports below.
    async _op_eval({code}) {
        const AsyncFunction = (async () => {}).constructor;
        const ext = Main.extensionManager.lookup(TARGET).stateObj;
        return await new AsyncFunction('Main', 'Clutter', 'GLib', 'Gio', 'Shell', 'ext', code)(
            Main, Clutter, GLib, Gio, Shell, ext);
    }

    get _ext() {
        return Main.extensionManager.lookup(TARGET).stateObj;
    }

    _op_inspect() {
        const {_blob: blob, _caption: caption, _blobMenu: blobMenu, _indicator: indicator} = this._ext;
        if (!blob)
            return {enabled: false};
        return {
            enabled: true,
            blob: {
                visible: blob.visible,
                mapped: blob.actor.mapped,
                reactive: blob.actor.reactive,
                box: box(blob.actor),
                theme: blob._shown?.meta.id ?? null,
                values: blob._values,
                timeline: blob._timeline.is_playing(),
            },
            caption: {
                visible: caption.actor.visible,
                opacity: caption.actor.opacity,
                reactive: caption.actor.reactive,
                focusable: caption.actor.can_focus,
                box: box(caption.actor),
                heard: caption._heard.text,
                reply: caption._reply.text,
                error: caption._reply.has_style_class_name('tuxistant-caption-error'),
            },
            blobMenu: {
                open: blobMenu.menu.isOpen,
                items: items(blobMenu.menu),
                themes: blobMenu._themes.menu._getMenuItems().map(i => [i.label.text, i._ornament]),
            },
            indicator: {
                open: indicator.button.menu.isOpen,
                style: indicator.button.get_style_class_name(),
                items: items(indicator.button.menu),
                themesShown: indicator._themes.visible,
                themes: indicator._themes.menu._getMenuItems().map(i => [i.label.text, i._ornament]),
                recent: items(indicator._recent),
                offlineLine: indicator._offline.visible,
            },
            keyFocus: global.stage.key_focus?.name ?? global.stage.key_focus?.toString() ?? null,
        };
    }

    _op_counts() {
        return {
            uiGroup: Main.uiGroup.get_n_children(),
            tracked: Main.layoutManager._trackedActors.length,
            statusArea: Boolean(Main.panel.statusArea[TARGET]),
            keybinding: Boolean(Main.wm._allowedKeybindings['toggle-shortcut']),
            enabled: Boolean(this._ext?._blob),
            state: Main.extensionManager.lookup(TARGET).state,
        };
    }

    async _setEnabled(enabled) {
        const uuid = TARGET;
        if (enabled)
            Main.extensionManager.enableExtension(uuid);
        else
            Main.extensionManager.disableExtension(uuid);
        for (let i = 0; i < 100; i++) {
            if ((Main.extensionManager.lookup(uuid).state === ACTIVE) === enabled)
                return;
            await sleep(20);  // eslint-disable-line no-await-in-loop
        }
        throw new Error(`extension did not reach enabled=${enabled}`);
    }

    async _op_cycle({count}) {
        const rounds = [];
        for (let i = 0; i < count; i++) {
            /* eslint-disable no-await-in-loop */
            await this._setEnabled(false);
            const off = this._op_counts();
            await sleep(150);
            await this._setEnabled(true);
            await sleep(150);
            /* eslint-enable no-await-in-loop */
            rounds.push({off, on: this._op_counts()});
        }
        return rounds;
    }

    async _op_set_enabled({enabled}) {
        await this._setEnabled(enabled);
    }

    _op_pick({x, y}) {
        const actor = global.stage.get_actor_at_pos(Clutter.PickMode.REACTIVE, x, y);
        return {name: actor?.name ?? null, type: actor?.constructor.name ?? null};
    }

    async _op_click({x, y, button}) {
        // One for the whole run: a collected device takes the pointer focus with it, at random.
        this._pointer ??= Clutter.get_default_backend().get_default_seat()
            .create_virtual_device(Clutter.InputDeviceType.POINTER_DEVICE);
        const pointer = this._pointer;
        const time = () => GLib.get_monotonic_time();
        pointer.notify_absolute_motion(time(), x, y);
        await sleep(50);
        pointer.notify_button(time(), button, Clutter.ButtonState.PRESSED);
        await sleep(30);
        pointer.notify_button(time(), button, Clutter.ButtonState.RELEASED);
        await sleep(30);
    }

    async _op_keys({keyvals}) {
        this._keyboard ??= Clutter.get_default_backend().get_default_seat()
            .create_virtual_device(Clutter.InputDeviceType.KEYBOARD_DEVICE);
        const keyboard = this._keyboard;
        const time = () => GLib.get_monotonic_time();
        for (const key of keyvals) {
            keyboard.notify_keyval(time(), key, Clutter.KeyState.PRESSED);
            await sleep(30);  // eslint-disable-line no-await-in-loop
        }
        for (const key of [...keyvals].reverse()) {
            keyboard.notify_keyval(time(), key, Clutter.KeyState.RELEASED);
            await sleep(30);  // eslint-disable-line no-await-in-loop
        }
    }

    _op_open_indicator() {
        this._ext._indicator.button.menu.open();
    }

    _op_close_indicator() {
        this._ext._indicator.button.menu.close();
    }

    _op_activate({menu, label}) {
        const ext = this._ext;
        const roots = {
            blob: [ext._blobMenu.menu],
            theme: [ext._blobMenu._themes.menu],
            indicatorTheme: [ext._indicator._themes.menu],
            indicator: [ext._indicator.button.menu],
            recent: [ext._indicator._recent],
        }[menu];
        const item = roots[0]._getMenuItems().find(i => i.label?.text === label);
        if (!item)
            throw new Error(`no menu item ${label} in ${menu}`);
        item.activate(Clutter.get_current_event());
    }

    _op_clipboard() {
        return new Promise(resolve => {
            St.Clipboard.get_default().get_text(St.ClipboardType.CLIPBOARD,
                (_clipboard, text) => resolve({text}));
        });
    }

    async _op_shoot({path}) {
        const stream = Gio.File.new_for_path(path).replace(null, false, Gio.FileCreateFlags.NONE, null);
        const shooter = new Shell.Screenshot();
        await new Promise((resolve, reject) => {
            shooter.screenshot(false, stream, (_s, result) => {
                try {
                    shooter.screenshot_finish(result);
                    resolve();
                } catch (e) {
                    reject(e);
                }
            });
        });
        stream.close(null);
    }
}
