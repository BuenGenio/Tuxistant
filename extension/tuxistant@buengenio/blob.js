// The blob: one actor that hosts the active theme's fragment shader.
import Clutter from 'gi://Clutter';
import Cogl from 'gi://Cogl';
import GLib from 'gi://GLib';
import GObject from 'gi://GObject';
import Shell from 'gi://Shell';
import St from 'gi://St';

import * as Main from 'resource:///org/gnome/shell/ui/main.js';

const UNIFORMS = `
uniform float u_time;
uniform float u_level;
uniform float u_appear;
uniform float u_listen;
uniform float u_think;
uniform float u_speak;
uniform vec2 u_size;
uniform vec2 tux_target;
`;

// Clutter's offscreen target is the actor plus a fixed pad, 2 logical pixels left and top, 1 right
// and bottom (_clutter_actor_box_enlarge_for_effects), and the quad is drawn over all of it. Map
// back to the actor box so uv is exact, and leave the pad transparent.
const PAD_BEFORE = 2;
const PAD_TOTAL = 3;
const WRAPPER = `
vec2 tux_uv = (cogl_tex_coord_in[0].xy * tux_target - vec2(${PAD_BEFORE}.0)) /
    (tux_target - vec2(${PAD_TOTAL}.0));
vec2 tux_inside = step(vec2(0.0), tux_uv) * step(tux_uv, vec2(1.0));
cogl_color_out = tux_theme(tux_uv) * (u_appear * tux_inside.x * tux_inside.y);
`;

const SCALARS = ['u_time', 'u_level', 'u_appear', 'u_listen', 'u_think', 'u_speak'];
const PREVIEW_STATES = ['listening', 'thinking', 'speaking'];
const PREVIEW_SECONDS = 4;
const LEVEL_STALE_SECONDS = 0.2;
const DEFAULT_MARGIN = 32;

// Shell.GLSLEffect caches its pipeline per GType and a GType can never be unregistered, so each
// theme (and each revision of its shader) gets one class that lives as long as the shell.
const effectClasses = new Map();

function effectClassFor(id, source) {
    const hash = GLib.compute_checksum_for_string(GLib.ChecksumType.SHA256, source, -1).slice(0, 16);
    const key = `${id}:${hash}`;
    if (effectClasses.has(key))
        return effectClasses.get(key);

    // #line makes compiler errors point at the theme's own file: "1:<line>" in the shell log.
    const declarations = `${UNIFORMS}\n#line 0 1\n${source}\n`;
    const klass = GObject.registerClass({
        GTypeName: `TuxistantEffect_${id.replaceAll('-', '_')}_${hash}`,
    }, class extends Shell.GLSLEffect {
        _init(params) {
            super._init(params);
            this._locations = Object.fromEntries(
                [...SCALARS, 'u_size', 'tux_target'].map(n => [n, this.get_uniform_location(n)]));
        }

        vfunc_build_pipeline() {
            this.add_glsl_snippet(Cogl.SnippetHook.FRAGMENT, declarations, WRAPPER, false);
        }

        setScalar(name, value) {
            this.set_uniform_float(this._locations[name], 1, [value]);
        }

        setSize(width, height) {
            this.set_uniform_float(this._locations.u_size, 2, [width, height]);
            this.set_uniform_float(this._locations.tux_target, 2,
                [width + PAD_TOTAL, height + PAD_TOTAL]);
        }
    });
    effectClasses.set(key, klass);
    return klass;
}

// Frame-rate independent exponential approach.
function approach(value, target, tau, dt) {
    return value + (target - value) * (1 - Math.exp(-dt / tau));
}

const now = () => GLib.get_monotonic_time() / 1e6;

export class Blob {
    // onContextMenu fires on a right click, which only orb layouts can receive. onLayout fires
    // when the actor moves, resizes, shows or hides.
    constructor({onContextMenu, onLayout}) {
        this._onLayout = onLayout;
        this.actor = new St.Widget({name: 'tuxistant-blob', visible: false, reactive: false});
        this.actor.connect('button-press-event', (_actor, event) => {
            if (event.get_button() !== Clutter.BUTTON_SECONDARY)
                return Clutter.EVENT_PROPAGATE;
            onContextMenu();
            return Clutter.EVENT_STOP;
        });
        Main.layoutManager.addTopChrome(this.actor);
        Main.layoutManager.connectObject('monitors-changed', () => this._place(), this);

        this._timeline = new Clutter.Timeline({
            actor: this.actor,
            duration: 1000,
            repeat_count: -1,
        });
        this._frameId = this._timeline.connect('new-frame', () => this._onFrame());
        // The shell disposes the stage on exit without disabling extensions, and an unstopped
        // timeline goes on ticking into a dead actor.
        this.actor.connect('destroy', () => this._timeline?.stop());

        this._monitor = null;
        this._active = null;   // {meta, source} chosen by config
        this._preview = null;  // the same plus start time, while ThemePreview runs
        this._shown = null;    // what the effect was built from
        this._effect = null;
        this._wanted = false;
        this._held = false;    // its menu is open, so it stays even if the turn ends
        this._running = false;
        this._state = 'idle';
        this._levelTarget = 0;
        this._levelAt = 0;
        this._values = {u_time: 0, u_level: 0, u_appear: 0, u_listen: 0, u_think: 0, u_speak: 0};
    }

    get visible() {
        return this._running;
    }

    get rect() {
        if (!this._shown)
            return null;
        const [x, y] = this.actor.get_position();
        const [width, height] = this.actor.get_size();
        return {x, y, width, height};
    }

    get anchor() {
        const layout = this._shown?.meta.layout;
        return layout?.kind === 'orb' ? layout.anchor : 'bottom-center';
    }

    get monitor() {
        return this._monitor;
    }

    setTheme(theme) {
        this._active = theme;
        if (!this._preview)
            this._show(theme);
        this._sync();
    }

    setWanted(wanted) {
        this._wanted = wanted;
        if (wanted && this._preview) {
            this._preview = null;
            if (this._active)
                this._show(this._active);
        }
        this._sync();
    }

    setHeld(held) {
        this._held = held;
        this._sync();
    }

    setState(state) {
        this._state = state;
    }

    setLevel(level) {
        this._levelTarget = Math.min(1, Math.max(0, level));
        this._levelAt = now();
    }

    // Plays a theme for a few seconds without touching the real state. A real turn takes over.
    preview(theme) {
        if (this._wanted)
            return;
        this._preview = {...theme, start: now()};
        this._show(this._preview);
        this._sync();
    }

    _show(theme) {
        const same = this._shown?.source === theme.source && this._shown.meta.id === theme.meta.id;
        this._shown = theme;
        if (!same) {
            if (this._effect)
                this.actor.remove_effect(this._effect);
            const Effect = effectClassFor(theme.meta.id, theme.source);
            this._effect = new Effect();
            this.actor.add_effect_with_name('tuxistant-theme', this._effect);
        }
        // While hidden it has no place: _sync finds one, on the monitor in use by then.
        if (this._running)
            this._place();
        this._pushValues();
    }

    _place() {
        const layout = this._shown?.meta.layout;
        const layoutManager = Main.layoutManager;
        const monitor = layoutManager.currentMonitor ?? layoutManager.primaryMonitor;
        if (!layout || !monitor)
            return;

        this._monitor = monitor;
        if (layout.kind === 'band') {
            const height = Math.round(monitor.height * Number(layout.height));
            this.actor.set_position(monitor.x, monitor.y + monitor.height - height);
            this.actor.set_size(monitor.width, height);
            this.actor.reactive = false;
        } else {
            const area = layoutManager.getWorkAreaForMonitor(monitor.index);
            const width = Number(layout.size);
            const height = Number(layout.height);
            const margin = Number(layout.margin ?? DEFAULT_MARGIN);
            const [vertical, horizontal = 'center'] = layout.anchor.split('-');
            this.actor.set_position({
                left: area.x + margin,
                center: area.x + Math.round((area.width - width) / 2),
                right: area.x + area.width - width - margin,
            }[horizontal], {
                top: area.y + margin,
                center: area.y + Math.round((area.height - height) / 2),
                bottom: area.y + area.height - height - margin,
            }[vertical]);
            this.actor.set_size(width, height);
            this.actor.reactive = true;
        }
        this._effect?.setSize(...this.actor.get_size());
        this._onLayout();
    }

    _sync() {
        const want = (this._wanted || this._held || this._preview !== null) && this._shown !== null;
        if (!want || this._running)
            return;
        this._running = true;
        this._lastFrame = now();
        this._place();
        this.actor.show();
        global.compositor.disable_unredirect();
        this._timeline.start();
    }

    _stop() {
        this._running = false;
        this._timeline.stop();
        this.actor.hide();
        global.compositor.enable_unredirect();
        this._values.u_appear = 0;
        if (this._active)
            this._show(this._active);
        this._onLayout();
    }

    _onFrame() {
        const time = now();
        const dt = Math.min(time - this._lastFrame, 0.1);
        this._lastFrame = time;

        let state = this._state;
        let level = time - this._levelAt > LEVEL_STALE_SECONDS ? 0 : this._levelTarget;
        if (this._preview) {
            const t = time - this._preview.start;
            if (t >= PREVIEW_SECONDS) {
                this._preview = null;
            } else {
                const phase = Math.floor(t / (PREVIEW_SECONDS / PREVIEW_STATES.length));
                state = PREVIEW_STATES[phase];
                level = state === 'thinking' ? 0
                    : 0.45 + 0.3 * Math.sin(t * 9) + 0.15 * Math.sin(t * 23);
            }
        }

        const want = this._wanted || this._held || this._preview !== null;
        const v = this._values;
        v.u_time += dt;
        v.u_level = approach(v.u_level, level, level > v.u_level ? 0.04 : 0.16, dt);
        v.u_appear = approach(v.u_appear, want ? 1 : 0, want ? 0.14 : 0.09, dt);
        v.u_listen = approach(v.u_listen, state === 'listening' ? 1 : 0, 0.15, dt);
        v.u_think = approach(v.u_think, state === 'thinking' || state === 'connecting' ? 1 : 0, 0.15, dt);
        v.u_speak = approach(v.u_speak, state === 'speaking' ? 1 : 0, 0.15, dt);
        if (want && v.u_appear > 0.999)
            v.u_appear = 1;

        if (!want && v.u_appear < 0.004) {
            this._stop();
            return;
        }
        this._pushValues();
        this.actor.queue_redraw();
    }

    _pushValues() {
        if (!this._effect)
            return;
        for (const name of SCALARS)
            this._effect.setScalar(name, this._values[name]);
    }

    destroy() {
        Main.layoutManager.disconnectObject(this);
        this._active = null;
        this._onLayout = () => {};
        if (this._running)
            this._stop();
        this._timeline.disconnect(this._frameId);
        this._timeline = null;
        this._effect = null;
        this.actor.destroy();
        this.actor = null;
    }
}
