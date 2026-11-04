#!/usr/bin/env python3
"""Test only: opens the real Settings window, prints what its pages filled in with, and closes."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from gi.repository import GLib  # noqa: E402
from tuxistant import settings_app as sa  # noqa: E402

app = sa.SettingsApp()


def chosen(rows):
    return [row.value for row in rows if row.check.get_opacity() == 1]


def report():
    window = app.get_active_window()
    if not window or window.state != "ready" or not window.history.rows:
        return GLib.SOURCE_CONTINUE
    voices = [row for _group, rows in window.voices.groups for row, _voice in rows]
    print(json.dumps({
        "server": window.general.server.get_text(),
        "voices": len(voices), "voice": chosen(voices),
        "themes": [row.value for row in window.appearance.rows],
        "theme": chosen(window.appearance.rows),
        "problems": len(window.appearance.problem_rows),
        "skills": [row.get_title() for row in window.skills.rows],
        "history": [row.get_title() for row in window.history.rows],
    }), flush=True)
    window.close()
    return GLib.SOURCE_REMOVE


GLib.timeout_add(100, report)
sys.exit(app.run(sys.argv[:1]))
