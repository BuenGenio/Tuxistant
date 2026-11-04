// Thin async client for org.tuxistant.Daemon. Nothing here blocks or logs when it is offline.
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';

const BUS_NAME = 'org.tuxistant.Daemon';
const BUS_PATH = '/org/tuxistant/Daemon';

export class Daemon {
    // onOnline(bool) fires on every owner change; onSignal(name, args) for every daemon signal.
    constructor({onOnline, onSignal}) {
        this._onOnline = onOnline;
        this._onSignal = onSignal;
        this._cancellable = new Gio.Cancellable();
        this._proxy = null;
        // Activation stays with explicit calls: enabling the extension must not launch the daemon.
        Gio.DBusProxy.new_for_bus(
            Gio.BusType.SESSION,
            Gio.DBusProxyFlags.DO_NOT_LOAD_PROPERTIES |
                Gio.DBusProxyFlags.DO_NOT_AUTO_START_AT_CONSTRUCTION,
            null, BUS_NAME, BUS_PATH, BUS_NAME, this._cancellable,
            (_source, result) => this._onProxy(result));
    }

    _onProxy(result) {
        let proxy;
        try {
            proxy = Gio.DBusProxy.new_for_bus_finish(result);
        } catch (e) {
            if (!e.matches(Gio.IOErrorEnum, Gio.IOErrorEnum.CANCELLED))
                this._onOnline(false);
            return;
        }
        this._proxy = proxy;
        this._signalId = proxy.connect('g-signal',
            (_p, _sender, name, params) => this._onSignal(name, params.deepUnpack()));
        this._ownerId = proxy.connect('notify::g-name-owner', () => this._onOnline(this.online));
        this._onOnline(this.online);
    }

    get online() {
        return this._proxy?.g_name_owner != null;
    }

    get cancellable() {
        return this._cancellable;
    }

    // Resolves with the unpacked reply tuple. May activate the daemon.
    call(method, args = null, timeoutMs = 5000) {
        return new Promise((resolve, reject) => {
            if (!this._proxy) {
                reject(new Error('daemon proxy not ready'));
                return;
            }
            this._proxy.call(method, args, Gio.DBusCallFlags.NONE, timeoutMs, this._cancellable,
                (proxy, result) => {
                    try {
                        resolve(proxy.call_finish(result).deepUnpack());
                    } catch (e) {
                        reject(e);
                    }
                });
        });
    }

    // Stops whatever is going on, on the way out. Not through the proxy: destroy() cancels what the
    // proxy has in flight, and this has to get there.
    stopNow() {
        this._lastWord('Cancel');
    }

    // Nothing here can draw a card or type any more, and dictation must not go on without a face.
    goodbye() {
        this._lastWord('Hello', new GLib.Variant('(i)', [0]));
    }

    _lastWord(method, args = null) {
        Gio.DBus.session.call(BUS_NAME, BUS_PATH, BUS_NAME, method, args, null,
            Gio.DBusCallFlags.NO_AUTO_START, -1, null, () => {});
    }

    // For fire-and-forget actions: failures are returned to onError, never logged.
    send(method, args = null, onError = null) {
        this.call(method, args).catch(e => {
            if (!e.matches?.(Gio.IOErrorEnum, Gio.IOErrorEnum.CANCELLED))
                onError?.(e);
        });
    }

    destroy() {
        this._cancellable.cancel();
        if (this._proxy) {
            this._proxy.disconnect(this._signalId);
            this._proxy.disconnect(this._ownerId);
            this._proxy = null;
        }
        this._onOnline = this._onSignal = () => {};
    }
}
