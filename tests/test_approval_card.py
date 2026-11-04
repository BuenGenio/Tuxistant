"""The approval card in an isolated real GNOME Shell, with real clicks (see shell_harness/card.sh)."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

try:
    from PIL import Image
except ImportError:
    Image = None

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tests.shell_harness.test_extension_headless import write_theme  # noqa: E402

CARD = Path(__file__).resolve().parent / "shell_harness" / "card.sh"
EDGE = 12
GAP = 14
MAX_SHARE = 0.7
LINE = 18
TAIL = "THE-LAST-WORDS"
SECRET = "the-selected-text-that-must-stay-out-of-logs"
BAND_QUARTER_TOP = 900 - 225

needs_shell = unittest.skipUnless(shutil.which("gnome-shell") and Image, "needs gnome-shell and PIL")


def write_themes(root):
    write_theme(root, "solid-orb", "Solid orb",
                {"kind": "orb", "size": 300, "anchor": "bottom-right", "margin": 32})
    write_theme(root, "band-quarter", "Band quarter", {"kind": "band", "height": 0.25})
    write_theme(root, "band-half", "Band half", {"kind": "band", "height": 0.5})
    write_theme(root, "band-full", "Band full", {"kind": "band", "height": 1.0})
    write_theme(root, "orb-top-left", "Orb top left",
                {"kind": "orb", "size": 200, "anchor": "top-left", "margin": 20})
    write_theme(root, "orb-center", "Orb center",
                {"kind": "orb", "size": 300, "anchor": "center", "margin": 20})
    write_theme(root, "orb-huge", "Orb huge",
                {"kind": "orb", "size": 1024, "anchor": "bottom-right", "margin": 0})


def start(out, themes, scenario, monitors="1600x900", scale=1):
    return subprocess.Popen(
        [str(CARD), "solid-orb", str(out)],
        env={**os.environ, "TUX_EXTRA_THEMES": str(themes), "TUX_SCENARIO": scenario,
             "TUX_MONITORS": monitors, "TUX_SCALE": str(scale), "TUX_TIMEOUT": "170"},
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)


def read_report(out):
    path = Path(out) / "report.json"
    return json.loads(path.read_text()) if path.exists() else {}


def inside(box, outer, margin=0):
    return (box["x"] >= outer["x"] + margin and box["y"] >= outer["y"] + margin
            and box["x"] + box["width"] <= outer["x"] + outer["width"] - margin
            and box["y"] + box["height"] <= outer["y"] + outer["height"] - margin)


def overlap(a, b):
    return (a["x"] < b["x"] + b["width"] and b["x"] < a["x"] + a["width"]
            and a["y"] < b["y"] + b["height"] and b["y"] < a["y"] + a["height"])


class ShellRun(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="tux-card-")
        cls.root = Path(cls.tmp.name)
        cls.themes = cls.root / "themes"
        cls.themes.mkdir()
        write_themes(cls.themes)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()


@needs_shell
class Card(ShellRun):
    """One run on 1600x900 that goes through everything. Alone, as it times clicks against a clock."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.out = cls.root / "out"
        run = start(cls.out, cls.themes, "verify")
        cls.log, _ = run.communicate(timeout=200)
        cls.code = run.returncode
        cls.report = read_report(cls.out)

    def image(self, name):
        return Image.open(self.out / f"{name}.png").convert("RGB")

    def test_00_loads_and_runs_with_no_errors(self):
        self.assertEqual(self.code, 0, self.log)

    def test_allow_starts_disabled_and_deny_does_not(self):
        fresh = self.report["fresh"]
        self.assertFalse(fresh["allow"]["reactive"])
        self.assertTrue(fresh["allow"]["insensitive"])
        self.assertFalse(fresh["allow"]["hit"], "a click would reach Allow")
        self.assertTrue(fresh["deny"]["reactive"])
        self.assertTrue(fresh["deny"]["hit"])
        armed = self.report["armed"]
        self.assertTrue(armed["allow"]["reactive"])
        self.assertTrue(armed["allow"]["hit"])
        self.assertFalse(armed["allow"]["insensitive"])

    def test_an_early_click_on_allow_does_nothing(self):
        early = self.report["early_click"]
        self.assertFalse(early["card"]["allow"]["reactive"], "too slow to prove anything: rerun")
        self.assertEqual(early["answers"], [])
        self.assertTrue(early["card"]["showing"])

    def test_a_press_from_before_it_armed_does_not_count(self):
        held = self.report["held_from_before_armed"]
        self.assertTrue(held["card"]["allow"]["reactive"], "the button should have armed meanwhile")
        self.assertEqual(held["answers"], [["Answer", "normal", True]])

    def test_clicking_is_what_answers(self):
        allow, deny = self.report["after_allow"], self.report["after_deny"]
        self.assertEqual(allow["answers"], [["Answer", "normal", True]])
        self.assertFalse(allow["card"]["showing"])
        self.assertEqual(deny["answers"], [["Answer", "normal", True], ["Answer", "held", False]])
        self.assertFalse(deny["card"]["showing"])

    def test_nothing_but_a_left_click_on_a_button_answers(self):
        for name in ("right_click", "click_on_card"):
            self.assertEqual(len(self.report[name]["answers"]), 2, name)
            self.assertTrue(self.report[name]["card"]["showing"], name)

    def test_the_keyboard_cannot_reach_it(self):
        r = self.report
        self.assertEqual(r["after_keys"]["answers"], [])
        self.assertTrue(r["after_keys"]["card"]["showing"])
        self.assertEqual(r["after_keys"]["card"]["keyFocus"], r["before"]["keyFocus"])
        self.assertEqual(r["armed"]["keyFocus"], r["before"]["keyFocus"])
        for part in (r["armed"], r["armed"]["allow"], r["armed"]["deny"]):
            self.assertFalse(part["focusable"])
        self.assertTrue(r["armed"]["reactive"])

    def test_a_window_keeps_its_focus_and_its_input(self):
        r = self.report
        for name in ("window_focus_card_up", "window_focus_after_window_click",
                     "window_focus_after_card_click"):
            self.assertEqual(r[name], "card-probe", name)
        self.assertTrue(r["window_card_still_up"])
        self.assertEqual([line for line in r["window_input"] if line],
                         ["key 97", "key 98", "click", "key 99"])

    def test_it_shows_over_a_fullscreen_window_and_takes_a_click(self):
        r = self.report
        self.assertEqual(r["fullscreen"], {"fullscreen": True, "inFullscreen": True})
        self.assertTrue(r["fullscreen_card"]["allow"]["hit"])
        self.assertEqual(r["fullscreen_answers"], [["Answer", "fullscreen", True]])

    def test_admin_looks_and_reads_different(self):
        r = self.report
        self.assertTrue(r["admin"]["admin"])
        self.assertEqual(r["admin"]["allow"]["label"], "Allow as administrator")
        self.assertFalse(r["armed"]["admin"])
        self.assertEqual(r["armed"]["allow"]["label"], "Allow")
        self.assertEqual(r["after_admin"]["answers"][-1], ["Answer", "admin", True])

        def colour(name, card):
            button = card["allow"]
            return self.image(name).getpixel((button["x"] + 6, button["y"] + button["height"] // 2))
        red, green, blue = colour("admin", r["admin"])
        self.assertGreater(red, 2 * blue, "admin Allow is not a warning colour")
        red, green, blue = colour("normal", r["armed"])
        self.assertGreater(blue, 2 * red, "plain Allow should not look like a warning")
        border = self.image("admin").getpixel((r["admin"]["box"]["x"] + 40, r["admin"]["box"]["y"] + 1))
        self.assertGreater(border[0], 200, "no warning border")

    def test_an_unknown_level_gets_the_warning(self):
        self.assertTrue(self.report["unknown_level"]["admin"])

    def test_a_long_detail_is_all_there_and_can_be_read_to_the_end(self):
        r = self.report
        for start_name, end_name in (("long", "long_end"), ("unbroken", "unbroken_end")):
            top, end = r[start_name], r[end_name]
            self.assertTrue(top["detail"].endswith(TAIL), start_name)
            self.assertFalse(top["ellipsized"], start_name)
            self.assertGreater(top["scroll"]["upper"], top["scroll"]["page"])
            self.assertTrue(top["more"]["visible"], "nothing says there is more")
            self.assertEqual(top["more"]["opacity"], 255)
            self.assertTrue(inside(top["box"], top["area"], EDGE), start_name)
            # Real wheel events got to the end, and only there does the note go.
            self.assertEqual(end["scroll"]["value"] + end["scroll"]["page"], end["scroll"]["upper"])
            self.assertEqual(end["more"]["opacity"], 0)
            self.assertEqual(end["box"], top["box"], "the card moved while scrolling")
        partway = r["long_some_way"]
        self.assertGreater(partway["scroll"]["value"], 0)
        self.assertEqual(partway["more"]["opacity"], 255)

    def test_a_short_detail_needs_no_scrolling(self):
        armed = self.report["armed"]
        self.assertEqual(armed["scroll"]["upper"], armed["scroll"]["page"])
        self.assertFalse(armed["more"]["visible"])

    def test_text_is_never_markup(self):
        r = self.report
        title, detail = r["markup_sent"]
        self.assertEqual(r["markup"]["title"], title)
        self.assertEqual(r["markup"]["detail"], detail)
        self.assertFalse(r["markup"]["titleMarkup"])
        self.assertFalse(r["markup"]["detailMarkup"])

    def test_no_detail_means_no_empty_box(self):
        card = self.report["no_detail"]
        self.assertLess(card["box"]["height"], self.report["armed"]["box"]["height"] - 40)

    def test_only_its_own_close_takes_it_down_and_at_once(self):
        self.assertTrue(self.report["other_close"]["showing"])
        self.assertFalse(self.report["closed"]["showing"])

    def test_it_shows_the_time_and_ends_with_it_without_answering(self):
        r = self.report
        self.assertEqual(r["time_start"], "Denied in 3 s")
        self.assertEqual(r["time_later"], "Denied in 2 s")
        self.assertTrue(r["time_almost"]["showing"])
        self.assertFalse(r["time_over"]["showing"])
        self.assertEqual(r["answers_on_timeout"], [])

    def test_a_busy_shell_does_not_use_up_the_wait(self):
        self.assertFalse(self.report["after_stall"]["allow"]["reactive"])
        self.assertTrue(self.report["after_stall_armed"]["allow"]["reactive"])

    def test_a_card_that_replaces_another_starts_unarmed(self):
        for name in ("replaced_after_close", "replaced_directly"):
            case = self.report[name]
            self.assertTrue(case["old"]["allow"]["reactive"], name)
            self.assertEqual(case["fresh"]["box"], case["old"]["box"], "the point is the same place")
            self.assertFalse(case["fresh"]["allow"]["reactive"], name)
            self.assertEqual(case["early"], [], name)
            self.assertTrue(case["armed"]["allow"]["reactive"], name)
            self.assertEqual(case["answers"], [["Answer", f"{name}-2", True]])
            self.assertFalse(case["after"]["showing"])

    def test_beside_the_orb(self):
        card = self.report["with_orb"]
        blob = card["blob"]["box"]
        self.assertEqual(card["box"]["x"] + card["box"]["width"], blob["x"] + blob["width"])
        self.assertEqual(card["box"]["y"] + card["box"]["height"] + GAP, blob["y"])

    def test_a_long_card_stops_short_of_the_orb(self):
        card = self.report["long"]
        self.assertFalse(overlap(card["box"], card["blob"]["box"]))

    def test_above_a_band_and_clickable(self):
        r = self.report
        for name in ("with_band", "with_band_long"):
            card = r[name]
            self.assertEqual(card["blob"]["box"]["y"], BAND_QUARTER_TOP)
            self.assertEqual(card["blob"]["box"]["width"], 1600)
            self.assertFalse(overlap(card["box"], card["blob"]["box"]), name)
            self.assertTrue(inside(card["box"], card["area"], EDGE), name)
            self.assertLess(abs(card["box"]["x"] + card["box"]["width"] / 2 - 800), 2, name)
        self.assertEqual(r["band_answers"], [["Answer", "band", True]])

    def test_over_a_band_as_tall_as_the_screen(self):
        r = self.report
        card = r["with_full_band"]
        self.assertTrue(inside(card["box"], card["area"], EDGE))
        self.assertTrue(card["aboveBlob"])
        self.assertEqual(r["full_band_answers"], [["Answer", "full", False]])

    def test_with_no_blob_it_goes_in_the_corner(self):
        card = self.report["no_blob"]
        self.assertFalse(card["blob"]["visible"])
        self.assertTrue(inside(card["box"], card["area"], EDGE))
        area = card["area"]
        self.assertGreater(card["box"]["x"] + card["box"]["width"], area["x"] + area["width"] - 40)
        self.assertGreater(card["box"]["y"] + card["box"]["height"], area["y"] + area["height"] - 40)

    def test_it_stays_put_when_the_theme_changes(self):
        change = self.report["theme_change"]
        self.assertEqual(change["after"]["box"], change["before"]["box"])
        self.assertTrue(change["after"]["allow"]["reactive"])

    def test_after_the_monitors_change_it_has_to_be_seen_again(self):
        self.assertFalse(self.report["monitors_changed"]["allow"]["reactive"])
        self.assertTrue(self.report["monitors_changed_armed"]["allow"]["reactive"])

    def test_it_works_in_the_overview(self):
        self.assertTrue(self.report["overview"]["allow"]["hit"])
        self.assertEqual(self.report["overview_answers"], [["Answer", "overview", True]])

    def test_it_is_on_top(self):
        card = self.report["armed"]
        self.assertTrue(card["top"])
        self.assertTrue(card["aboveCaption"])

    def test_the_caption_is_never_up_with_a_card(self):
        r = self.report
        self.assertTrue(r["caption_before"]["caption"]["visible"])
        self.assertEqual(r["caption_before"]["caption"]["heard"], "delete the old reports")
        for name in ("caption_hidden", "caption_held"):
            self.assertTrue(r[name]["showing"], name)
            self.assertFalse(r[name]["caption"]["visible"], name)

    def test_what_came_during_a_card_shows_after_it(self):
        r = self.report
        after = r["caption_after_card"]
        self.assertFalse(after["showing"])
        self.assertTrue(after["caption"]["visible"])
        self.assertEqual(after["caption"]["reply"], "This error waits for the card.")
        self.assertTrue(after["caption"]["error"])
        self.assertEqual(r["held_reply"]["caption"]["reply"], "Said while the card was up.")

    def test_after_a_card_the_caption_goes_on_as_before(self):
        r = self.report
        self.assertFalse(r["error_timer_ran"]["caption"]["visible"], "the error stayed")
        self.assertEqual(r["reply_after_card"]["caption"]["reply"], "Done, I moved them.")
        self.assertEqual(r["reply_after_card"]["caption"]["opacity"], 255)
        self.assertTrue(r["reply_lingers"]["caption"]["visible"])
        self.assertFalse(r["reply_gone"]["caption"]["visible"], "the reply never went away")
        self.assertFalse(r["held_reply_gone"]["caption"]["visible"], "the held reply never went away")

    def test_selection_prefers_primary_then_clipboard(self):
        r = self.report
        self.assertEqual(r["selection_nothing"], [["SelectionText", "nothing", ""]])
        self.assertEqual(r["selection_clipboard"], [["SelectionText", "clipboard", f"clipboard {SECRET}"]])
        self.assertEqual(r["selection_primary"], [["SelectionText", "primary", f"primary {SECRET}"]])

    def test_a_huge_selection_is_cut_to_what_a_bus_message_can_take(self):
        self.assertEqual(self.report["selection_huge"], [["SelectionText", "huge", 100000]])

    def test_selected_text_is_not_logged(self):
        self.assertNotIn(SECRET, (self.out / "shell.log").read_text(errors="replace"))

    def test_going_offline_takes_it_down_for_good(self):
        r = self.report
        self.assertTrue(r["before_offline"]["showing"])
        self.assertFalse(r["offline"]["showing"])
        self.assertFalse(r["back_online"]["showing"])
        self.assertEqual(r["answers_offline"], [])

    def test_disabling_takes_it_down_and_leaves_nothing_behind(self):
        r = self.report
        before, up = r["counts_before"], r["counts_card_up"]
        self.assertEqual(up["uiGroup"] - before["uiGroup"], 1)
        self.assertEqual(up["tracked"] - before["tracked"], 1)
        # The same four actors and two tracked ones that test_extension_headless counts.
        self.assertEqual(before["uiGroup"] - r["counts_disabled"]["uiGroup"], 4)
        self.assertEqual(before["tracked"] - r["counts_disabled"]["tracked"], 2)
        self.assertEqual(r["counts_reenabled"], before)
        self.assertEqual(r["answers_disable"], [])
        self.assertFalse(r["reenabled"]["showing"])


@needs_shell
class Fit(ShellRun):
    """Every blob and every kind of request on monitors from tiny to huge, side by side."""

    RUNS = {"tiny": ("320x240", 1), "small": ("640x480", 1), "laptop-at-2x": ("1366x768", 2),
            "4k": ("3840x2160", 1)}

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        runs = {name: start(cls.root / name, cls.themes, "fit", monitors, scale)
                for name, (monitors, scale) in cls.RUNS.items()}
        cls.logs = {name: run.communicate(timeout=200)[0] for name, run in runs.items()}
        cls.codes = {name: run.returncode for name, run in runs.items()}
        cls.reports = {name: read_report(cls.root / name) for name in runs}

    def cases(self):
        for name, report in self.reports.items():
            for case, card in report.items():
                yield f"{name} {case}", card

    def test_00_runs_with_no_errors(self):
        for name, code in self.codes.items():
            self.assertEqual(code, 0, f"{name}\n{self.logs[name]}")

    def test_every_case_ran(self):
        for name, report in self.reports.items():
            self.assertEqual(len(report), 25, name)

    def test_it_fits_with_room_to_spare(self):
        for name, card in self.cases():
            with self.subTest(name):
                self.assertTrue(card["showing"])
                self.assertTrue(inside(card["box"], card["area"], EDGE), card["box"])
                if not name.startswith("tiny"):  # there it may have all of a very small screen
                    self.assertLessEqual(card["box"]["height"], card["area"]["height"] * MAX_SHARE)

    def test_no_text_pushes_the_buttons_off_the_card(self):
        for name, card in self.cases():
            with self.subTest(name):
                for part in ("allow", "deny"):
                    self.assertTrue(inside(card[part], card["box"]), part)
                self.assertTrue(card["deny"]["hit"])

    def test_the_text_stays_on_the_card(self):
        for name, card in self.cases():
            with self.subTest(name):
                self.assertTrue(inside(card["detailBox"], card["box"]))
                if not card["titleScrolls"]:
                    self.assertTrue(inside(card["titleBox"], card["box"]))

    def test_some_of_the_detail_can_be_read(self):
        for name, card in self.cases():
            with self.subTest(name):
                self.assertGreaterEqual(card["detailBox"]["height"], 2 * LINE)

    def test_the_title_keeps_its_place_on_any_real_screen(self):
        for name, card in self.cases():
            with self.subTest(name):
                self.assertEqual(card["titleScrolls"], name.startswith("tiny"))

    def test_only_a_narrow_card_goes_without_the_time(self):
        for name, card in self.cases():
            with self.subTest(name):
                self.assertEqual(card["timeShown"], not name.startswith("tiny"))


@needs_shell
class RealDaemon(ShellRun):
    """The production service and controller against the real extension."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.out = cls.root / "out"
        run = start(cls.out, cls.themes, "real")
        cls.log, _ = run.communicate(timeout=200)
        cls.code = run.returncode
        cls.report = read_report(cls.out)

    def test_00_runs_with_no_errors(self):
        self.assertEqual(self.code, 0, self.log)

    def test_presence_gets_there_first_so_the_first_card_is_beside_the_blob(self):
        card = self.report["hostile"]
        self.assertTrue(card["blob"]["visible"])
        blob = card["blob"]["box"]
        self.assertEqual(card["box"]["x"] + card["box"]["width"], blob["x"] + blob["width"])

    def test_what_printable_made_is_what_the_card_shows(self):
        card, (title, detail) = self.report["hostile"], self.report["hostile_sent"]
        self.assertEqual(card["title"], title)
        self.assertEqual(card["detail"], detail)
        self.assertIn("\\u202e", card["title"])
        self.assertIn("\\u001b", card["detail"])
        self.assertNotIn("\u202e", card["title"])
        self.assertIn("more characters)", card["detail"])
        self.assertTrue(card["more"]["visible"])

    def test_a_click_is_what_the_controller_gets(self):
        r = self.report
        self.assertIs(r["allow_result"], True)
        self.assertFalse(r["after_allow"]["showing"])
        self.assertIs(r["deny_result"], False)
        self.assertIs(r["admin_result"], True)
        self.assertEqual(r["admin"]["allow"]["label"], "Allow as administrator")

    def test_the_next_action_cannot_ride_on_a_click_meant_for_the_last(self):
        self.assertFalse(self.report["next_fresh"]["allow"]["reactive"])
        self.assertTrue(self.report["next_waits"])

    def test_a_cancel_takes_the_card_down(self):
        self.assertFalse(self.report["cancelled"]["showing"])

    def test_no_answer_is_a_no_and_the_card_goes(self):
        r = self.report
        self.assertEqual(r["short_card"]["time"], "Denied in 2 s")
        self.assertIs(r["timeout_result"], False)
        self.assertFalse(r["timed_out"]["showing"])
        self.assertIs(r["presence_after"], False)

    def test_selection_reaches_the_controller(self):
        self.assertEqual(self.report["selection_clipboard"], f"clipboard {SECRET}")
        self.assertEqual(self.report["selection_primary"], f"primary {SECRET}")


@needs_shell
class TwoMonitors(ShellRun):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        run = start(cls.root / "out", cls.themes, "monitors", monitors="1280x720 1024x768")
        cls.log, _ = run.communicate(timeout=200)
        cls.code = run.returncode
        cls.report = read_report(cls.root / "out")

    def test_00_runs_with_no_errors(self):
        self.assertEqual(self.code, 0, self.log)

    def test_with_no_blob_it_goes_where_the_pointer_is(self):
        r = self.report
        self.assertEqual(r["no_blob_second"]["monitor"]["index"], 1)
        self.assertEqual(r["no_blob_first"]["monitor"]["index"], 0)
        for name in ("no_blob_second", "no_blob_first"):
            self.assertTrue(inside(r[name]["box"], r[name]["area"], EDGE), name)

    def test_with_a_blob_it_stays_with_the_blob(self):
        card = self.report["blob_second_pointer_first"]
        self.assertEqual(card["pointerMonitor"], 0)
        self.assertEqual(card["monitor"]["index"], 1)
        self.assertTrue(inside(card["box"], card["area"], EDGE))


if __name__ == "__main__":
    unittest.main()
