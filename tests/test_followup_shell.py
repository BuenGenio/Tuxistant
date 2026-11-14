"""Follow-ups in an isolated real GNOME Shell, through the real service and controller (see
shell_harness/card.sh): a question stays on screen with how to answer it."""
import unittest

from tests.test_approval_card import ShellRun, needs_shell, read_report, start

HINT = "Hold Fn to answer"
QUESTION = "I found two reports. Which one should I print?"


@needs_shell
class Question(ShellRun):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.out = cls.root / "out"
        run = start(cls.out, cls.themes, "followup_real")
        cls.log, _ = run.communicate(timeout=260)
        cls.code = run.returncode
        cls.report = read_report(cls.out)

    def test_00_runs_with_no_errors(self):
        self.assertEqual(self.code, 0, self.log)
        self.assertEqual(self.report["state"], "idle")

    def test_the_blob_was_let_go_before_the_question(self):
        self.assertFalse(self.report["after_release_presence"])

    def test_a_question_brings_back_the_blob_with_the_hint(self):
        for key in ("question_up", "question_still_up", "second_question_up", "third_question_up",
                    "fourth_question_up"):
            with self.subTest(key):
                look = self.report[key]
                self.assertTrue(look["blob"], look)
                self.assertTrue(look["caption"], look)
                self.assertEqual(look["reply"], QUESTION)
                self.assertEqual(look["hint"], HINT)
                self.assertEqual(look["state"], "idle")

    def test_it_goes_by_itself_after_about_ten_seconds(self):
        look = self.report["question_gone"]
        self.assertFalse(look["blob"], look)
        self.assertFalse(look["caption"], look)

    def test_holding_fn_again_takes_the_hint_down(self):
        look = self.report["answering"]
        self.assertEqual(look["state"], "listening")
        self.assertEqual(look["hint"], "")
        self.assertNotEqual(look["reply"], QUESTION)

    def test_a_go_shows_its_own_hint_in_place_of_the_question(self):
        look = self.report["dictating_over_question"]
        self.assertEqual(look["state"], "listening")
        self.assertEqual(look["heard"], "Dictating… tap Fn to stop")
        self.assertEqual(look["hint"], "")
        self.assertNotEqual(look["reply"], QUESTION)

    def test_a_plain_reply_leaves_no_hint(self):
        look = self.report["plain_reply"]
        self.assertFalse(look["blob"], look)
        self.assertEqual(look["hint"], "")

    def test_cancel_takes_the_question_down(self):
        look = self.report["cancelled"]
        self.assertFalse(look["blob"], look)
        self.assertFalse(look["caption"], look)

    def test_every_hold_used_the_one_connection(self):
        self.assertEqual(self.report["connections"], 1)
        self.assertEqual(self.report["asked"], ["print my report", "print my other report",
                                                "what time is it", "print the last one",
                                                "print the very last one"])


if __name__ == "__main__":
    unittest.main()
