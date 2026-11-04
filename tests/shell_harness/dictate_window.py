#!/usr/bin/env python3
"""A window for the dictation scenario: one widget, and a file that always holds what it holds.

`slow` takes 1.2 s to react to Insert. `owner` takes the clipboard, then stops answering for 8 s
once a file named <path>.hang appears, as a hung app does.
"""
import os
import sys
import time

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gdk, GLib, Gtk  # noqa: E402

path, kind = sys.argv[1], sys.argv[2]


def note(text):
    with open(path, "w", encoding="utf-8") as out:
        out.write(text)


def widget():
    if kind == "password":
        field = Gtk.PasswordEntry()
        field.connect("changed", lambda f: note(f.get_text()))
        return field
    if kind == "button":
        return Gtk.Button(label="Nothing to type in")
    field = Gtk.TextView()
    field.get_buffer().connect("changed", lambda b: note(b.props.text))
    if kind == "slow":
        slow_down(field)
    if kind == "owner":
        take_the_clipboard(field)
    return field


def slow_down(field):
    def pressed(_keys, keyval, _code, _state):
        if keyval == Gdk.KEY_Insert:
            time.sleep(1.2)
        return False

    keys = Gtk.EventControllerKey(propagation_phase=Gtk.PropagationPhase.CAPTURE)
    keys.connect("key-pressed", pressed)
    field.add_controller(keys)


def take_the_clipboard(field):
    def own():
        field.get_clipboard().set_content(Gdk.ContentProvider.new_for_bytes(
            "text/plain;charset=utf-8", GLib.Bytes.new(b"owned-by-a-slow-app")))
        return False

    def hang_if_told():
        if os.path.exists(path + ".hang"):
            os.unlink(path + ".hang")
            time.sleep(8)
        return True

    GLib.timeout_add(700, own)
    GLib.timeout_add(50, hang_if_told)


def activate(app):
    window = Gtk.ApplicationWindow(application=app, title=f"dictate-{kind}",
                                   default_width=700, default_height=400)
    child = widget()
    window.set_child(child)
    window.present()
    child.grab_focus()


note("")
app = Gtk.Application(application_id=f"org.tuxistant.Dictate{kind.capitalize()}")
app.connect("activate", activate)
app.run([])
