"""Dictation in an isolated real GNOME Shell, into real GTK windows (see shell_harness/card.sh)."""
import unittest

from tests.test_approval_card import ShellRun, needs_shell, read_report, start


@needs_shell
class Typing(ShellRun):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.out = cls.root / "out"
        run = start(cls.out, cls.themes, "dictation")
        cls.log, _ = run.communicate(timeout=260)
        cls.code = run.returncode
        cls.report = read_report(cls.out)

    def test_00_runs_with_no_errors(self):
        self.assertEqual(self.code, 0, self.log)

    def test_words_of_any_language_reach_a_text_field_without_the_clipboard(self):
        self.assertEqual(self.report["typed_how"], "typed")
        self.assertEqual(self.report["typed_text"], "Hello there, Привіт, ñandú 你好! ")
        self.assertEqual(self.report["typed_clipboard"], "keep-me")

    def test_words_can_never_press_enter_or_escape(self):
        self.assertEqual(self.report["enter_how"], "typed")
        text = self.report["enter_text"]
        self.assertTrue(text.endswith("first second  [Athird fourthfifth "), repr(text))
        for character in "\n\r\x1b ‮":
            self.assertNotIn(character, text)

    def test_words_nobody_said_were_coming_go_nowhere(self):
        self.assertEqual(self.report["unexpected_text"], self.report["enter_text"])
        self.assertEqual(self.report["unexpected_how"], [None])

    def test_with_a_shell_dialog_up_they_are_copied_not_typed(self):
        self.assertEqual(self.report["modal_how"], "copied")
        self.assertEqual(self.report["modal_clipboard"], "over a shell dialog ")
        self.assertEqual(self.report["modal_text"], self.report["enter_text"])

    def test_after_the_focus_moves_they_are_copied_and_it_says_so(self):
        self.assertEqual(self.report["moved_how"], "copied")
        self.assertEqual(self.report["moved_clipboard"], "for the first window ")
        self.assertIn("copied", self.report["moved_note"])
        self.assertEqual(self.report["moved_first_window"], self.report["enter_text"])
        self.assertEqual(self.report["moved_other_window"], "")

    def test_the_joiners_that_are_spelling_reach_the_window(self):
        self.assertEqual(self.report["joiners_how"], "typed")
        self.assertEqual(self.report["joiners_text"], "می\u200cخواهم 👩\u200d💻 ")

    def test_two_goes_at_once_each_keep_their_place(self):
        self.assertEqual(self.report["overlap_how"], ["typed", "typed"])
        self.assertTrue(self.report["overlap_text"].endswith("one two "))

    def test_at_the_time_limit_it_copies_and_says_so(self):
        self.assertEqual(self.report["ran_out_how"], "copied")
        self.assertEqual(self.report["ran_out_clipboard"], "what the room said ")
        self.assertIn("five minutes", self.report["ran_out_note"])
        self.assertNotIn("what the room said", self.report["ran_out_window"])

    def test_a_long_passage_does_not_kill_the_app_and_all_of_it_arrives(self):
        self.assertTrue(self.report["long_alive"])
        self.assertEqual(self.report["long_how"], "pasted")
        self.assertEqual(self.report["long_added"], 5000)
        self.assertEqual(self.report["long_clipboard"], "keep-me")

    def test_a_long_passage_is_measured_in_bytes_not_letters(self):
        self.assertTrue(self.report["cyrillic_long_alive"])
        self.assertEqual(self.report["cyrillic_long_how"], "pasted")

    def test_a_password_box_stays_refused_behind_a_shell_dialog(self):
        self.assertEqual(self.report["password_modal_how"], "refused")
        self.assertEqual(self.report["password_modal_clipboard"], "keep-me")

    def test_a_picture_on_the_clipboard_comes_back(self):
        self.assertEqual(self.report["image_how"], "pasted")
        self.assertIn("image/png", self.report["image_mimetypes"])
        self.assertEqual(self.report["image_bytes"], [137, 80, 78, 71, 13, 10, 26, 10])

    def test_a_password_managers_copy_is_not_put_back(self):
        self.assertEqual(self.report["hint_how"], "pasted")
        self.assertIn(self.report["hint_clipboard"], ("", None))
        self.assertNotIn("x-kde-passwordManagerHint", self.report["hint_mimetypes"])

    def test_two_pastes_in_a_row_put_back_the_users_clipboard(self):
        self.assertEqual(self.report["bursts_how"], ["pasted", "pasted"])
        self.assertEqual(self.report["bursts_clipboard"], "first-clipboard")

    def test_a_slow_app_gets_the_words_and_not_the_old_clipboard(self):
        self.assertEqual(self.report["slow_how"], "pasted")
        self.assertEqual(self.report["slow_text"], "the dictated words ")

    def test_a_hung_clipboard_owner_cannot_hold_up_the_words(self):
        self.assertEqual(self.report["hung_how"], "pasted")
        self.assertLess(self.report["hung_seconds"], 1.0)
        self.assertEqual(self.report["hung_text"], "for this window ")

    def test_words_are_not_left_on_the_clipboard_after_a_failed_save(self):
        self.assertIn(self.report["hung_clipboard_after"], ("", None))

    def test_a_focus_change_during_the_save_stops_the_paste(self):
        self.assertEqual(self.report["save_moved_how"], "copied")
        self.assertEqual(self.report["save_moved_text"], "")
        self.assertEqual(self.report["save_moved_other"], "")
        self.assertEqual(self.report["save_moved_clipboard"], "for the window I was in ")

    def test_disabling_during_the_save_leaves_no_words_behind(self):
        self.assertNotEqual(self.report["disabled_clipboard"], "private words ")
        self.assertNotEqual(self.report["disabled_primary"], "private words ")

    def test_a_password_box_gets_nothing_and_nothing_is_kept(self):
        self.assertEqual(self.report["password_how"], "refused")
        self.assertEqual(self.report["password_text"], "")
        self.assertEqual(self.report["password_clipboard"], "keep-me")
        self.assertIn("password", self.report["password_note"])

    def test_an_app_with_no_input_method_gets_a_paste_and_the_clipboard_comes_back(self):
        self.assertEqual(self.report["paste_how"], "pasted")
        self.assertEqual(self.report["paste_text"], "Pasted words, ñandú Привіт ")
        self.assertEqual(self.report["paste_clipboard"], "before-clipboard")
        self.assertEqual(self.report["paste_primary"], "before-primary")

    def test_the_paste_works_on_a_layout_with_no_v(self):
        self.assertEqual(self.report["cyrillic_how"], "pasted")
        self.assertTrue(self.report["cyrillic_text"].endswith("on a Cyrillic layout "))

    def test_ctrl_v_would_not_have_worked_there(self):
        """Why the paste is Shift+Insert. If this ever fails, Ctrl+V has become an option."""
        self.assertNotIn("CTRLV", self.report["cyrillic_ctrl_v_text"])

    def test_something_copied_during_the_paste_is_left_alone(self):
        self.assertEqual(self.report["meanwhile_clipboard"], "copied-meanwhile")

    def test_with_no_field_a_paste_does_nothing_and_fires_no_shortcuts(self):
        self.assertEqual(self.report["button_how"], "pasted")
        self.assertEqual(self.report["button_text"], "")
        self.assertEqual(self.report["button_clipboard"], "keep-me")

    def test_with_no_window_at_all_they_are_copied(self):
        self.assertEqual(self.report["no_window_how"], "copied")
        self.assertEqual(self.report["no_window_clipboard"], "no window at all ")


@needs_shell
class ThroughTheRealDaemon(ShellRun):
    """The real service, guard and controller with the real extension, so the join is what's tested."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.out = cls.root / "out"
        run = start(cls.out, cls.themes, "dictation_real")
        cls.log, _ = run.communicate(timeout=260)
        cls.code = run.returncode
        cls.report = read_report(cls.out)

    def test_00_runs_with_no_errors(self):
        self.assertEqual(self.code, 0, self.log)
        self.assertEqual(self.report["errors"], [])
        self.assertEqual(self.report["state"], "idle")

    def test_the_extension_says_it_can_type_and_the_guard_lets_it(self):
        self.assertTrue(self.report["typist"])

    def test_words_travel_the_whole_way_to_a_real_window(self):
        self.assertEqual(self.report["held_text"], "Real words, Привіт. ")

    def test_the_shortcut_can_stop_it_from_the_overview(self):
        self.assertTrue(self.report["overview_listening"])
        self.assertTrue(self.report["overview_stopped"])

    def test_turning_the_extension_off_turns_off_the_mic(self):
        self.assertTrue(self.report["disabled_stopped"])
        self.assertFalse(self.report["disabled_typist"])

    def test_the_shortcut_starts_it_and_stops_it(self):
        self.assertTrue(self.report["shortcut_started"])
        self.assertEqual(self.report["shortcut_text"], "Real words, Привіт. From the shortcut. ")


if __name__ == "__main__":
    unittest.main()
