// The right-click menu on the blob and the status icon in the panel.
import GLib from 'gi://GLib';
import St from 'gi://St';

import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as PanelMenu from 'resource:///org/gnome/shell/ui/panelMenu.js';
import * as PopupMenu from 'resource:///org/gnome/shell/ui/popupMenu.js';

const RECENT_LIMIT = 8;
const HEARD_CHARS = 44;
const STATES = ['connecting', 'listening', 'thinking', 'speaking', 'error'];

function truncate(text, max) {
    const flat = text.replace(/\s+/g, ' ').trim();
    return flat.length > max ? `${flat.slice(0, max - 1)}…` : flat;
}

function fillThemes(submenu, themes, activeId, onTheme) {
    submenu.menu.removeAll();
    for (const {id, name} of themes) {
        const item = new PopupMenu.PopupMenuItem(name);
        item.setOrnament(id === activeId ? PopupMenu.Ornament.DOT : PopupMenu.Ornament.NO_DOT);
        item.connect('activate', () => onTheme(id));
        submenu.menu.addMenuItem(item);
    }
}

export class BlobMenu {
    constructor(sourceActor, {onTheme, onSettings, onStop, onOpenChanged}) {
        this._onTheme = onTheme;
        this.menu = new PopupMenu.PopupMenu(sourceActor, 0.5, St.Side.BOTTOM);
        Main.uiGroup.add_child(this.menu.actor);
        this.menu.actor.hide();
        this._manager = new PopupMenu.PopupMenuManager(sourceActor);
        this._manager.addMenu(this.menu);

        this._themes = new PopupMenu.PopupSubMenuMenuItem('Theme');
        this.menu.addMenuItem(this._themes);
        this.menu.addAction('Settings', onSettings);
        this.menu.addAction('Stop', onStop);
        this.menu.connect('open-state-changed', (_menu, open) => onOpenChanged(open));
    }

    open(themes, activeId) {
        fillThemes(this._themes, themes, activeId, this._onTheme);
        this.menu.open();
    }

    destroy() {
        this.menu.destroy();
        this.menu = null;
        this._manager = null;
    }
}

export class Indicator {
    constructor(role, daemon, {onTalk, onTheme, onSettings, onOpen}) {
        this._daemon = daemon;
        this._onTheme = onTheme;
        this._refreshes = 0;

        this.button = new PanelMenu.Button(0.0, 'Tuxistant', false);
        this.button.add_style_class_name('tuxistant-indicator');
        this.button.add_child(new St.Icon({
            icon_name: 'audio-input-microphone-symbolic',
            style_class: 'system-status-icon',
        }));

        const menu = this.button.menu;
        this._offline = new PopupMenu.PopupMenuItem('Tuxistant daemon is unreachable', {
            reactive: false,
            can_focus: false,
        });
        this._offline.add_style_class_name('tuxistant-offline-line');
        menu.addMenuItem(this._offline);
        this._talk = menu.addAction('Talk', onTalk);
        // The blob's own menu needs a blob: it is gone between turns, and a band lets clicks through.
        this._themes = new PopupMenu.PopupSubMenuMenuItem('Theme');
        this._themes.visible = false;
        menu.addMenuItem(this._themes);
        menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem('Recent'));
        this._recent = new PopupMenu.PopupMenuSection();
        menu.addMenuItem(this._recent);
        menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
        menu.addAction('Settings', onSettings);
        menu.connectObject('open-state-changed', (_menu, open) => {
            if (open) {
                this.refresh();
                onOpen();
            }
        }, this);

        Main.panel.addToStatusArea(role, this.button);
        this.setOnline(false);
        // Menus decide separator visibility when they open, so the section is never left empty.
        this._showRecent([]);
    }

    setOnline(online) {
        this._offline.visible = !online;
        if (online)
            this.button.remove_style_class_name('tuxistant-offline');
        else
            this.button.add_style_class_name('tuxistant-offline');
    }

    setState(state) {
        for (const name of STATES) {
            if (name === state)
                this.button.add_style_class_name(`tuxistant-state-${name}`);
            else
                this.button.remove_style_class_name(`tuxistant-state-${name}`);
        }
        this._talk.label.text = state === 'idle' ? 'Talk' : 'Stop';
    }

    setThemes(themes, activeId) {
        fillThemes(this._themes, themes, activeId, this._onTheme);
        this._themes.visible = themes.length > 0;
    }

    async refresh() {
        const refresh = ++this._refreshes;
        let requests = [];
        try {
            // Looking at the menu must not start a daemon that was stopped: calls activate it.
            if (this._daemon.online) {
                const [json] = await this._daemon.call('GetHistory',
                    new GLib.Variant('(i)', [RECENT_LIMIT]));
                requests = JSON.parse(json);
            }
        } catch {
            // Offline shows as its own line. The list is simply left empty.
        }
        if (refresh === this._refreshes && this._recent)
            this._showRecent(requests);
    }

    _showRecent(requests) {
        this._recent.removeAll();
        for (const {heard, reply} of requests.slice(0, RECENT_LIMIT)) {
            const item = new PopupMenu.PopupMenuItem(truncate(heard, HEARD_CHARS) || '(nothing heard)');
            item.setSensitive(reply !== '');
            item.connect('activate', () =>
                St.Clipboard.get_default().set_text(St.ClipboardType.CLIPBOARD, reply));
            this._recent.addMenuItem(item);
        }
        if (requests.length === 0) {
            const item = new PopupMenu.PopupMenuItem('No requests yet', {reactive: false});
            item.setSensitive(false);
            this._recent.addMenuItem(item);
        }
    }

    destroy() {
        this.button.menu.disconnectObject(this);
        this.button.destroy();
        this.button = null;
        this._recent = null;
        this._daemon = null;
    }
}
