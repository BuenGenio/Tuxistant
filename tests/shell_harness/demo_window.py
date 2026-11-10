#!/usr/bin/env python3
"""A notes window for the demo video's dictation scene, and a file that always holds what it holds.

    demo_window.py <file> <width> <height>
"""
import sys

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gdk, Gtk  # noqa: E402

path, width, height = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
MARGIN = 16


def note(text):
    with open(path, "w", encoding="utf-8") as out:
        out.write(text)


def style():
    Gtk.Settings.get_default().props.gtk_application_prefer_dark_theme = True
    provider = Gtk.CssProvider()
    provider.load_from_string("textview { font-size: 14pt; }")
    Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), provider,
                                              Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)


def activate(app):
    style()
    window = Gtk.ApplicationWindow(application=app, title="Notes", default_width=width, default_height=height)
    field = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR, left_margin=MARGIN, right_margin=MARGIN,
                         top_margin=MARGIN, bottom_margin=MARGIN)
    field.get_buffer().connect("changed", lambda b: note(b.props.text))
    window.set_child(field)
    window.present()
    field.grab_focus()


note("")
app = Gtk.Application(application_id="org.tuxistant.DemoNotes")
app.connect("activate", activate)
app.run([])
