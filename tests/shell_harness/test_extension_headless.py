"""End-to-end test of the extension in an isolated headless GNOME Shell (see shot.sh)."""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

try:
    from PIL import Image
except ImportError:
    Image = None

HERE = Path(__file__).resolve().parent
SHOT = HERE / "shot.sh"
BG = (58, 65, 80)  # shot.sh default #3a4150
PANEL = 32
SCREEN = (1600, 900)

# Yellow stripe along the top and red, green, half-alpha blue and white quadrants below it.
QUADRANTS = """
vec4 tux_theme(vec2 uv) {
    if (uv.y < 0.1) return vec4(1.0, 1.0, 0.0, 1.0);
    if (uv.x < 0.5 && uv.y < 0.5) return vec4(1.0, 0.0, 0.0, 1.0);
    if (uv.x >= 0.5 && uv.y < 0.5) return vec4(0.0, 1.0, 0.0, 1.0);
    if (uv.x < 0.5) return vec4(0.0, 0.0, 0.5, 0.5);
    return vec4(1.0);
}
"""


def write_theme(root, theme_id, name, layout):
    folder = root / theme_id
    folder.mkdir()
    meta = {"id": theme_id, "name": name, "author": "harness", "version": "1", "layout": layout}
    (folder / "theme.json").write_text(json.dumps(meta))
    (folder / "shader.glsl").write_text(QUADRANTS)


def write_themes(root):
    write_theme(root, "solid-orb", "Solid orb",
                {"kind": "orb", "size": 300, "anchor": "bottom-right", "margin": 32})
    write_theme(root, "solid-band", "Solid band", {"kind": "band", "height": 0.25})
    for anchor in ("bottom-right", "bottom-left", "top-right", "top-left", "bottom-center", "center"):
        write_theme(root, f"anchor-{anchor}", f"Anchor {anchor}",
                    {"kind": "orb", "size": 120, "anchor": anchor, "margin": 20})


def run_shell(theme, out, themes, scenario, **env):
    return subprocess.run(
        [str(SHOT), theme, str(out)],
        env={**os.environ, "TUX_EXTRA_THEMES": str(themes), "TUX_SCENARIO": scenario,
             "TUX_TIMEOUT": "170", **env},
        capture_output=True, text=True, timeout=200)


def close(a, b, tolerance=3):
    return all(abs(x - y) <= tolerance for x, y in zip(a, b))


@unittest.skipUnless(shutil.which("gnome-shell") and Image, "needs gnome-shell and PIL")
class HeadlessShell(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="tux-headless-")
        root = Path(cls.tmp.name)
        cls.out = root / "out"
        themes = root / "themes"
        themes.mkdir()
        write_themes(themes)
        cls.result = run_shell("solid-orb", cls.out, themes, "verify")
        cls.report = {}
        if (cls.out / "report.json").exists():
            cls.report = json.loads((cls.out / "report.json").read_text())

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def image(self, name):
        return Image.open(self.out / f"{name}.png").convert("RGB")

    def calls(self, name):
        return [c for c in self.report["calls"] if c[0] == name]

    def test_00_clean_run(self):
        self.assertEqual(self.result.returncode, 0, self.result.stdout + self.result.stderr)

    def test_visibility_needs_presence_and_active_state(self):
        r = self.report
        self.assertFalse(r["idle"]["blob"]["visible"])
        self.assertFalse(r["idle"]["blob"]["timeline"])
        self.assertFalse(r["presence_only"]["blob"]["visible"])
        self.assertFalse(r["state_only"]["blob"]["visible"])
        self.assertTrue(r["listening"]["blob"]["visible"])
        self.assertTrue(r["listening"]["blob"]["timeline"])
        self.assertGreater(r["listening"]["blob"]["values"]["u_listen"], 0.9)
        self.assertGreater(r["listening"]["blob"]["values"]["u_level"], 0.1)
        self.assertEqual(r["listening"]["blob"]["values"]["u_appear"], 1)

    def test_orb_orientation_alpha_and_placement(self):
        img = self.image("orb")
        x0, y0 = SCREEN[0] - 32 - 300, SCREEN[1] - 32 - 300
        self.assertEqual(self.report["listening"]["blob"]["box"],
                         {"x": x0, "y": y0, "width": 300, "height": 300})
        self.assertEqual(img.getpixel((x0 + 150, y0 + 5)), (255, 255, 0))      # top
        self.assertEqual(img.getpixel((x0 + 40, y0 + 80)), (255, 0, 0))        # top left
        self.assertEqual(img.getpixel((x0 + 260, y0 + 80)), (0, 255, 0))       # top right
        self.assertEqual(img.getpixel((x0 + 260, y0 + 260)), (255, 255, 255))  # bottom right
        # (0, 0, .5, .5) premultiplied over the background
        blended = (BG[0] / 2, BG[1] / 2, 127.5 + BG[2] / 2)
        self.assertTrue(close(img.getpixel((x0 + 40, y0 + 260)), blended))
        # The offscreen pad stays transparent: the edges are exactly the actor's.
        self.assertEqual(img.getpixel((x0 - 1, y0 + 100)), BG)
        self.assertEqual(img.getpixel((x0, y0 + 100)), (255, 0, 0))
        self.assertEqual(img.getpixel((x0 + 299, y0 + 100)), (0, 255, 0))
        self.assertEqual(img.getpixel((x0 + 300, y0 + 100)), BG)
        self.assertEqual(img.getpixel((x0 + 100, y0 - 1)), BG)
        self.assertEqual(img.getpixel((x0 + 100, y0)), (255, 255, 0))
        self.assertEqual(img.getpixel((x0 + 260, y0 + 299)), (255, 255, 255))
        self.assertEqual(img.getpixel((x0 + 260, y0 + 300)), BG)

    def test_anchors(self):
        w, h, s, m = SCREEN[0], SCREEN[1], 120, 20
        expected = {
            "bottom-right": (w - s - m, h - s - m), "bottom-left": (m, h - s - m),
            "top-right": (w - s - m, PANEL + m), "top-left": (m, PANEL + m),
            "bottom-center": ((w - s) // 2, h - s - m),
            "center": ((w - s) // 2, PANEL + (h - PANEL - s) // 2),
        }
        for anchor, (x, y) in expected.items():
            box = self.report["anchors"][anchor]
            self.assertEqual((box["x"], box["y"], box["width"]), (x, y, s), anchor)

    def test_band_is_bottom_full_width_and_click_through(self):
        r = self.report
        height = round(SCREEN[1] * 0.25)
        self.assertEqual(r["band"]["blob"]["theme"], "solid-band")
        self.assertEqual(r["band"]["blob"]["box"],
                         {"x": 0, "y": SCREEN[1] - height, "width": SCREEN[0], "height": height})
        self.assertFalse(r["band"]["blob"]["reactive"])
        self.assertNotEqual(r["pick_band"]["name"], "tuxistant-blob")
        self.assertFalse(r["band_right_click"]["blobMenu"]["open"])
        img = self.image("band")
        top = SCREEN[1] - height
        self.assertEqual(img.getpixel((100, top - 1)), BG)
        self.assertEqual(img.getpixel((5, top)), (255, 255, 0))
        self.assertEqual(img.getpixel((SCREEN[0] - 1, top + 60)), (0, 255, 0))
        self.assertEqual(img.getpixel((0, top + 60)), (255, 0, 0))
        self.assertEqual(img.getpixel((SCREEN[0] - 1, SCREEN[1] - 1)), (255, 255, 255))

    def test_orb_is_reactive(self):
        self.assertEqual(self.report["pick_orb"]["name"], "tuxistant-blob")

    def test_caption(self):
        r = self.report
        self.assertEqual(r["heard_partial"]["caption"]["heard"], "turn on")
        self.assertEqual(r["heard_final"]["caption"]["heard"], "turn on the lights please")
        self.assertTrue(r["reply"]["caption"]["reply"].startswith("Done. The lights"))
        self.assertEqual(r["reply"]["caption"]["opacity"], 255)
        self.assertTrue(r["reply_lingers"]["caption"]["visible"])
        self.assertFalse(r["reply_gone"]["caption"]["visible"])
        self.assertFalse(r["reply"]["caption"]["reactive"])
        self.assertFalse(r["reply"]["caption"]["focusable"])
        self.assertNotEqual(r["pick_caption"]["type"], "St_BoxLayout")
        self.assertEqual(r["reply"]["keyFocus"], r["idle"]["keyFocus"])
        # Near the orb: right edges line up, card sits just above it.
        cap, blob = r["reply"]["caption"]["box"], r["listening"]["blob"]["box"]
        self.assertEqual(cap["x"] + cap["width"], blob["x"] + blob["width"])
        self.assertEqual(cap["y"] + cap["height"] + 14, blob["y"])

    def test_error_shows_without_presence(self):
        r = self.report
        self.assertFalse(r["released"]["blob"]["visible"])
        self.assertFalse(r["released"]["caption"]["visible"])
        self.assertTrue(r["error"]["caption"]["visible"])
        self.assertEqual(r["error"]["caption"]["reply"], "Microphone not found")
        self.assertTrue(r["error"]["caption"]["error"])

    def test_context_menu(self):
        r = self.report
        self.assertTrue(r["context_menu"]["blobMenu"]["open"])
        self.assertEqual(r["context_menu"]["blobMenu"]["items"][1:], ["Settings", "Stop"])
        themes = dict(r["context_menu"]["blobMenu"]["themes"])
        self.assertEqual(themes["Solid orb"], 1)   # Ornament.DOT
        self.assertEqual(themes["Solid band"], 4)  # Ornament.NO_DOT
        self.assertEqual(r["set_config"], [["SetConfig", "theme", '"solid-band"']])
        self.assertEqual(r["cancels_from_the_menu"], 1)
        self.assertGreaterEqual(len(self.calls("OpenSettings")), 1)

    def test_the_blob_stays_while_its_menu_is_open(self):
        r = self.report
        self.assertTrue(r["menu_in_turn"]["blobMenu"]["open"])
        after = r["menu_after_turn"]
        self.assertTrue(after["blobMenu"]["open"], "the menu went with the turn")
        self.assertTrue(after["blob"]["mapped"])
        self.assertFalse(r["menu_closed"]["blob"]["visible"], "the blob stayed after the menu")

    def test_the_panel_icon_has_the_themes_between_turns(self):
        r = self.report
        menu = r["indicator_themes"]["indicator"]
        self.assertTrue(menu["themesShown"])
        themes = dict(menu["themes"])
        self.assertEqual(themes["Solid orb"], 1)   # Ornament.DOT
        self.assertIn("Solid band", themes)
        self.assertFalse(r["indicator_themes"]["blob"]["visible"])
        self.assertEqual(r["set_config_from_panel"], ["SetConfig", "theme", '"solid-band"'])

    def test_disabling_stops_what_is_going_on(self):
        self.assertIn(["Cancel"], self.report["calls_on_disable"])

    def test_a_stale_state_does_not_stick(self):
        blob = self.report["stale_refresh"]["blob"]
        self.assertFalse(blob["visible"], "the turn ended during _refresh, and the blob stayed")
        self.assertFalse(blob["timeline"])

    def test_an_offline_menu_calls_nothing(self):
        self.assertEqual(self.report["calls_from_offline_menu"], 0)

    def test_status_menu(self):
        r = self.report
        self.assertEqual(len(r["indicator_open"]["indicator"]["recent"]), 8)
        self.assertIn(["GetHistory", 8], r["calls"])
        self.assertEqual(r["clipboard"]["text"], "Reply 11")
        self.assertEqual(r["indicator_refreshed"]["indicator"]["recent"][0],
                         "Newest request after a change")
        self.assertFalse(r["indicator_open"]["indicator"]["offlineLine"])
        self.assertGreaterEqual(len(self.calls("Toggle")), 1)
        self.assertEqual(len(self.calls("OpenSettings")), 2)
        for state in ("listening", "thinking", "speaking"):
            self.assertIn(f"tuxistant-state-{state}", r["styles"][state])
        self.assertNotIn("tuxistant-state", r["styles"]["idle"])

    def test_hotkey(self):
        self.assertEqual(self.report["hotkey_toggles"], 1)

    def test_preview(self):
        r = self.report
        listening = r["preview_listening"]["blob"]
        self.assertTrue(listening["visible"])
        self.assertEqual(listening["theme"], "solid-band")
        self.assertGreater(listening["values"]["u_listen"], 0.5)
        self.assertGreater(r["preview_thinking"]["blob"]["values"]["u_think"], 0.5)
        self.assertGreater(r["preview_speaking"]["blob"]["values"]["u_speak"], 0.5)
        self.assertFalse(r["preview_over"]["blob"]["visible"])
        self.assertFalse(r["preview_over"]["blob"]["timeline"])
        self.assertEqual(r["preview_over"]["blob"]["theme"], "solid-orb")
        self.assertEqual(r["preview_calls"], [])
        self.assertEqual(r["preview_state"], "idle")

    def test_offline(self):
        r = self.report
        self.assertFalse(r["offline"]["blob"]["visible"])
        self.assertIn("tuxistant-offline", r["offline"]["indicator"]["style"])
        self.assertTrue(r["offline_menu"]["indicator"]["offlineLine"])
        self.assertEqual(r["offline_menu"]["indicator"]["recent"], ["No requests yet"])
        self.assertIn("unreachable", r["offline_talk"]["caption"]["reply"])
        self.assertNotIn("tuxistant-offline", r["back_online"]["indicator"]["style"])
        self.assertTrue(r["back_listening"]["blob"]["visible"])

    def test_enable_disable_cycle(self):
        r = self.report
        on, off = r["counts_enabled"], r["counts_disabled"]
        for cycle in r["cycles"]:
            self.assertEqual(cycle["on"], on)
            self.assertEqual(cycle["off"], off)
        self.assertTrue(on["statusArea"] and on["keybinding"] and on["enabled"])
        self.assertFalse(off["statusArea"] or off["keybinding"] or off["enabled"])
        # Blob, caption, blob menu and panel menu are what the extension adds to uiGroup.
        self.assertEqual(on["uiGroup"] - off["uiGroup"], 4)
        self.assertEqual(on["tracked"] - off["tracked"], 2)
        self.assertEqual(r["hotkey_after_disable"], [])
        self.assertTrue(r["reenabled"]["blob"]["visible"])
        self.assertEqual(self.image("reenabled").getpixel((1300, 650)), (255, 0, 0))


@unittest.skipUnless(shutil.which("gnome-shell") and Image, "needs gnome-shell and PIL")
class ConcurrentAndScaled(unittest.TestCase):
    def test_two_runs_at_once_one_at_scale_two(self):
        with tempfile.TemporaryDirectory(prefix="tux-headless-") as tmp:
            root = Path(tmp)
            themes = root / "themes"
            themes.mkdir()
            write_theme(themes, "small", "Small",
                        {"kind": "orb", "size": 100, "anchor": "bottom-right", "margin": 20})
            runs = {}
            for scale in (1, 2):
                runs[scale] = subprocess.Popen(
                    [str(SHOT), "small", str(root / str(scale))],
                    env={**os.environ, "TUX_EXTRA_THEMES": str(themes), "TUX_SCALE": str(scale)},
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            for run in runs.values():
                log, _ = run.communicate(timeout=120)
                self.assertEqual(run.returncode, 0, log)
            for scale in (1, 2):
                img = Image.open(root / str(scale) / "speaking.png").convert("RGB")
                self.assertEqual(img.size, SCREEN)
                size, margin = 100 * scale, 20 * scale
                x0, y0 = SCREEN[0] - margin - size, SCREEN[1] - margin - size
                self.assertEqual(img.getpixel((x0 - 1, y0 + 30 * scale)), BG, scale)
                self.assertEqual(img.getpixel((x0, y0 + 30 * scale)), (255, 0, 0), scale)
                self.assertEqual(img.getpixel((x0 + size - 1, y0 + 30 * scale)), (0, 255, 0), scale)
                self.assertEqual(img.getpixel((x0 + size, y0 + 30 * scale)), BG, scale)
                self.assertEqual(img.getpixel((x0 + 30 * scale, y0 - 1)), BG, scale)
                self.assertEqual(img.getpixel((x0 + 30 * scale, y0)), (255, 255, 0), scale)
                self.assertEqual(img.getpixel((x0 + size - 1, y0 + size - 1)), (255, 255, 255), scale)
                self.assertEqual(img.getpixel((x0 + size - 1, y0 + size)), BG, scale)


if __name__ == "__main__":
    unittest.main()
