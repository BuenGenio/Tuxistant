// Tuxistant for GNOME Shell. The daemon owns all logic; this is its face on the desktop.
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import Meta from 'gi://Meta';
import Shell from 'gi://Shell';

import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';

import {Approval, selectedText} from './approval.js';
import {Blob} from './blob.js';
import {Caption} from './caption.js';
import {Daemon} from './daemon.js';
import {BlobMenu, Indicator} from './menus.js';

const KEYBINDING = 'toggle-shortcut';
const UNREACHABLE = 'The Tuxistant daemon is unreachable';
const THEMES_TIMEOUT_MS = 30000;  // a theme not seen before gets test-compiled first
const CARDS = 1;  // what the daemon hears in Hello: this extension can draw approval cards

export default class TuxistantExtension extends Extension {
    enable() {
        this._themes = new Map();
        this._theme = null;  // id from config
        this._loads = 0;
        this._state = 'idle';
        this._stateSignals = 0;
        this._presence = false;

        this._blob = new Blob({
            onContextMenu: () => this._openBlobMenu(),
            onLayout: () => this._onBlobLayout(),
        });
        this._caption = new Caption();
        this._afterCard = [];
        this._approval = new Approval({
            onAnswer: (id, allow) => this._send('Answer', new GLib.Variant('(sb)', [id, allow])),
            onGone: () => this._onCardGone(),
        });
        this._daemon = new Daemon({
            onOnline: online => this._onOnline(online),
            onSignal: (name, args) => this._onSignal(name, args),
        });

        const settings = () => this._send('OpenSettings');
        const theme = id => this._send('SetConfig', new GLib.Variant('(ss)', ['theme', JSON.stringify(id)]));
        this._blobMenu = new BlobMenu(this._blob.actor, {
            onTheme: theme,
            onSettings: settings,
            onStop: () => this._send('Cancel'),
            onOpenChanged: open => this._blob.setHeld(open),
        });
        this._indicator = new Indicator(this.uuid, this._daemon, {
            onTalk: () => this._send('Toggle'),
            onTheme: theme,
            onSettings: settings,
            onOpen: () => this._fillIndicatorThemes(),
        });

        this._settings = this.getSettings();
        Main.wm.addKeybinding(KEYBINDING, this._settings, Meta.KeyBindingFlags.IGNORE_AUTOREPEAT,
            Shell.ActionMode.ALL, () => this._send('Toggle'));
    }

    disable() {
        Main.wm.removeKeybinding(KEYBINDING);
        // With the blob and the icon gone there would be nothing to show or stop a live microphone.
        if (this._state !== 'idle')
            this._daemon.stopNow();
        this._daemon.destroy();
        this._indicator.destroy();
        this._blobMenu.destroy();
        this._approval.destroy();  // the daemon's own timeout then answers no
        this._caption.destroy();
        this._blob.destroy();
        this._daemon = this._indicator = this._blobMenu = this._approval = null;
        this._caption = this._blob = null;
        this._themes = this._settings = null;
    }

    // Something the user asked for: only here does an unreachable daemon get mentioned on screen.
    _send(method, args = null) {
        this._daemon.send(method, args,
            () => this._toCaption(() => this._caption.showError(UNREACHABLE)));
    }

    _onOnline(online) {
        this._indicator.setOnline(online);
        if (online) {
            this._refresh();
        } else {
            this._approval.close();
            this._presence = false;
            this._setState('idle');
        }
    }

    async _refresh() {
        // Not awaited: being refused costs the approval cards, and must not cost the blob as well.
        this._daemon.call('Hello', new GLib.Variant('(i)', [CARDS]))
            .catch(e => this._reportUnlessOffline(e));
        try {
            const [config] = await this._daemon.call('GetConfig');
            this._theme = JSON.parse(config).theme;
            await this._refreshThemes();
            await this._loadActiveTheme();
            // State comes last, as the daemon only says when it changes: an answer that went stale
            // on the way here would stay until the next turn. A signal is always newer than this.
            const signals = this._stateSignals;
            const [state] = await this._daemon.call('GetState');
            if (signals !== this._stateSignals)
                return;
            // The contract has no way to ask for Presence, so a turn in progress implies it.
            this._presence ||= state !== 'idle';
            this._setState(state);
        } catch (e) {
            this._reportUnlessOffline(e);
        }
    }

    async _refreshThemes() {
        const [json] = await this._daemon.call('ListThemes', null, THEMES_TIMEOUT_MS);
        this._themes = new Map(JSON.parse(json).themes.map(theme => [theme.id, theme]));
    }

    _resolveTheme(id) {
        return this._themes.get(id) ?? this._themes.get('orb') ?? [...this._themes.values()][0];
    }

    async _loadActiveTheme() {
        const load = ++this._loads;
        const meta = this._resolveTheme(this._theme);
        if (!meta)
            return;
        const source = await this._readShader(meta);
        if (load === this._loads)
            this._blob.setTheme({meta, source});
    }

    _readShader(meta) {
        const file = Gio.File.new_for_path(GLib.build_filenamev([meta.path, 'shader.glsl']));
        return new Promise((resolve, reject) => {
            file.load_contents_async(this._daemon.cancellable, (_file, result) => {
                try {
                    const [, bytes] = file.load_contents_finish(result);
                    resolve(new TextDecoder().decode(bytes));
                } catch (e) {
                    const cancelled = e.matches(Gio.IOErrorEnum, Gio.IOErrorEnum.CANCELLED);
                    reject(cancelled ? e : new Error(`${file.get_path()}: ${e.message}`));
                }
            });
        });
    }

    // GLib errors here all come from the bus or from cancelling: being offline is not news.
    _reportUnlessOffline(e) {
        if (!(e instanceof GLib.Error))
            console.warn(`Tuxistant: ${e.message}`);
    }

    async _openBlobMenu() {
        // Held from here on: a turn that ends now must not pull the blob out from under the menu.
        this._blob.setHeld(true);
        try {
            await this._refreshThemes();
        } catch (e) {
            this._reportUnlessOffline(e);
        }
        this._blobMenu?.open([...this._themes.values()], this._resolveTheme(this._theme)?.id);
    }

    // Asking a daemon that is not running would start it, so an offline list stays as it was.
    async _fillIndicatorThemes() {
        try {
            if (this._daemon.online)
                await this._refreshThemes();
        } catch (e) {
            this._reportUnlessOffline(e);
        }
        this._indicator?.setThemes([...this._themes.values()], this._resolveTheme(this._theme)?.id);
    }

    _onBlobLayout() {
        const {rect, anchor, monitor, visible} = this._blob;
        this._caption?.follow(rect, anchor, monitor, visible);
        this._approval?.follow(rect, anchor, monitor, visible);
    }

    _setState(state) {
        const starting = this._state === 'idle' && state !== 'idle';
        this._state = state;
        this._indicator.setState(state);
        this._blob.setState(state);
        this._blob.setWanted(this._presence && state !== 'idle');
        if (starting)
            this._caption.reset();
        else if (state === 'idle')
            this._caption.linger();
    }

    // Text can arrive just after the turn ended, and showing it stops the hide timer.
    _lingerIfIdle() {
        if (this._state === 'idle')
            this._caption.linger();
    }

    // A card has that part of the screen to itself: what comes meanwhile waits until it has gone.
    _toCaption(show) {
        if (this._approval.showing)
            this._afterCard.push(show);
        else
            show();
    }

    _onCardGone() {
        const waiting = this._afterCard;
        this._afterCard = [];
        waiting.forEach(show => show());
        this._lingerIfIdle();
    }

    async _onSelectionWanted(id) {
        const text = await selectedText();
        if (this._daemon)
            this._send('SelectionText', new GLib.Variant('(ss)', [id, text]));
    }

    async _onThemePreview(id) {
        try {
            await this._refreshThemes();
            const meta = this._themes.get(id);
            if (meta)
                this._blob.preview({meta, source: await this._readShader(meta)});
        } catch (e) {
            this._reportUnlessOffline(e);
        }
    }

    async _onConfigChanged(key) {
        if (key !== 'theme')
            return;
        try {
            const [config] = await this._daemon.call('GetConfig');
            this._theme = JSON.parse(config).theme;
            await this._refreshThemes();
            await this._loadActiveTheme();
        } catch (e) {
            this._reportUnlessOffline(e);
        }
    }

    _onSignal(name, args) {
        switch (name) {
        case 'StateChanged':
            this._stateSignals++;
            this._setState(args[0]);
            break;
        case 'Presence':
            [this._presence] = args;
            this._blob.setWanted(this._presence && this._state !== 'idle');
            if (!this._presence)
                this._caption.linger();
            break;
        case 'Level':
            this._blob.setLevel(args[0]);
            break;
        case 'Transcript':
            if (this._presence)
                this._toCaption(() => this._caption.showHeard(args[0]));
            this._lingerIfIdle();
            break;
        case 'Reply':
            if (this._presence)
                this._toCaption(() => this._caption.showReply(args[0]));
            this._lingerIfIdle();
            break;
        case 'Error':
            this._toCaption(() => this._caption.showError(args[0]));
            break;
        case 'ConfigChanged':
            this._onConfigChanged(args[0]);
            break;
        case 'ThemePreview':
            this._onThemePreview(args[0]);
            break;
        case 'HistoryChanged':
            this._indicator.refresh();
            break;
        case 'Approval':
            this._caption.hide();
            this._approval.show(...args);
            break;
        case 'ApprovalClosed':
            this._approval.close(args[0]);
            break;
        case 'SelectionWanted':
            this._onSelectionWanted(args[0]);
            break;
        }
    }
}
