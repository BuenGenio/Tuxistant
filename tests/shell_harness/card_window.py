#!/usr/bin/env python3
"""A plain window for card.sh that writes down the input reaching it, one event per line."""
import sys

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk  # noqa: E402

log = open(sys.argv[1], "a", buffering=1)


def activate(app):
    window = Gtk.ApplicationWindow(application=app, title="card-probe",
                                   default_width=1000, default_height=640)
    keys = Gtk.EventControllerKey()
    keys.connect("key-pressed", lambda _c, keyval, _code, _state: log.write(f"key {keyval}\n") and False)
    window.add_controller(keys)
    clicks = Gtk.GestureClick()
    clicks.connect("pressed", lambda *_: log.write("click\n"))
    window.add_controller(clicks)
    window.present()


app = Gtk.Application(application_id="org.tuxistant.CardProbe")
app.connect("activate", activate)
app.run([])
