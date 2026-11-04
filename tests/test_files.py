"""file.find, file.open and file.trash, all inside a made-up home folder."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tuxistant import files  # noqa: E402
from tuxistant.gate import MAX_DETAIL_CHARS, Ask, Session, printable  # noqa: E402
from tuxistant.skills import BUILTIN, Skills  # noqa: E402

DAY = 86400


class MadeUpHome:
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name).resolve()
        self.home = self.root / "home"
        self.outside = self.root / "outside"
        self.outside.mkdir()
        for name in ("Desktop", "Documents", "Downloads", "Music", "Pictures", "Videos"):
            (self.home / name).mkdir(parents=True)
        patcher = mock.patch.dict(os.environ, {
            "HOME": str(self.home), "XDG_DATA_HOME": str(self.home / ".local/share"),
            "GIO_USE_VFS": "local"})
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("XDG_CONFIG_HOME", None)
        self.assertEqual(Path.home(), self.home)    # nothing below may reach the real one
        self.session = Session()

    def file(self, name: str, age: float = 0, size: int = 10, base: Path | None = None) -> Path:
        path = (base or self.home) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * size)
        then = time.time() - age * DAY
        os.utime(path, (then, then))
        return path

    def link(self, name: str, target: Path) -> Path:
        path = self.home / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.symlink_to(target)
        return path

    def assert_spoken(self, message: str):
        self.assertTrue(message.endswith((".", "?")), message)
        self.assertNotIn(str(self.root), message)
        self.assertNotIn("Traceback", message)


class Home(MadeUpHome, unittest.TestCase):
    pass


class Finding(Home):
    def index(self, *paths: Path):
        patcher = mock.patch.object(files, "_localsearch", return_value=list(paths))
        self.searched = patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(files, "_plocate", return_value=[])
        self.located = patcher.start()
        self.addCleanup(patcher.stop)

    def find(self, query="tax", **args) -> dict:
        return files.find({"query": query, **args}, None, self.session)

    def names(self, query="tax", **args) -> list[str]:
        return [r["name"] for r in self.find(query, **args)["results"]]

    def test_first_look_folders_lead_and_newer_files_go_first(self):
        self.index(self.file("Projects/tax-now.pdf"), self.file("tax-home.pdf", 1),
                   self.file("Documents/tax-old.pdf", 90), self.file("Downloads/tax-new.pdf", 3),
                   self.file("Desktop/tax-mid.pdf", 30))
        self.assertEqual(self.names(), ["tax-new.pdf", "tax-mid.pdf", "tax-old.pdf",
                                        "tax-now.pdf", "tax-home.pdf"])

    def test_other_peoples_files_come_last(self):
        self.index(self.file("app/node_modules/x/tax.pdf"), self.file("Projects/vendor/tax.pdf"),
                   self.file("Downloads/sdk/lib/tax.pdf"), self.file("old/tax-mine.pdf", 500))
        self.assertEqual(self.names()[0], "tax-mine.pdf")

    def test_a_copy_of_a_whole_system_comes_last(self):
        (self.home / "rootfs/etc").mkdir(parents=True)
        (self.home / "rootfs/usr").mkdir(parents=True)
        self.index(self.file("rootfs/Bosch/tax.pdf"), self.file("Projects/tax-mine.pdf", 500))
        self.assertEqual(self.names(), ["tax-mine.pdf", "tax.pdf"])

    def test_a_document_named_for_the_query_beats_a_mention(self):
        self.index(self.file("Documents/letter.pdf"), self.file("Projects/mortgage-offer.pdf", 40))
        self.assertEqual(self.names("mortgage"), ["mortgage-offer.pdf", "letter.pdf"])

    def test_code_named_for_the_query_does_not(self):
        self.index(self.file("Documents/letter.pdf", 40), self.file("Projects/mortgage.php"))
        self.assertEqual(self.names("mortgage"), ["letter.pdf", "mortgage.php"])

    def test_a_whole_word_beats_part_of_a_longer_one(self):
        self.index(self.file("Documents/taxi-ride.pdf"), self.file("Documents/tax-2024.pdf", 9))
        self.assertEqual(self.names(), ["tax-2024.pdf", "taxi-ride.pdf"])

    def test_hidden_and_outside_are_dropped(self):
        self.index(self.file(".cache/tax.pdf"), self.file("Documents/.private/tax.pdf"),
                   self.file(".tax"), self.file("tax.pdf", base=self.outside),
                   Path("/etc/tax.pdf"), self.file("Documents/tax-keep.pdf"))
        self.assertEqual(self.names(), ["tax-keep.pdf"])

    def test_folders_links_and_vanished_files_are_not_results(self):
        real = self.file("Documents/tax.pdf")
        self.index(self.home / "Documents", self.link("tax-link.pdf", real),
                   self.home / "gone.pdf", real)
        self.assertEqual(self.find()["count"], 1)
        self.assertEqual(self.session.found, [str(real)])

    def test_a_result_reached_through_a_link_out_of_home_is_not_kept(self):
        self.file("secret-tax.pdf", base=self.outside)
        self.link("Documents/away", self.outside)
        self.index(self.home / "Documents/away/secret-tax.pdf")
        self.assertEqual(self.find()["results"], [])
        self.assertEqual(self.session.found, [])

    def test_the_result_has_the_agreed_shape(self):
        path = self.file("Documents/Tax/SA100-2024.pdf", size=1_234_567)
        when = time.mktime((2025, 1, 12, 12, 0, 0, 0, 0, -1))
        os.utime(path, (when, when))
        self.index(path)
        self.assertEqual(self.find(), {"count": 1, "results": [{
            "n": 1, "name": "SA100-2024.pdf", "folder": "~/Documents/Tax",
            "modified": "12 January 2025", "size": "1.2 MB"}]})
        self.assertEqual(self.session.found, [str(path)])

    def test_it_all_turns_into_json(self):
        self.index(self.file("Documents/tax.pdf"))
        json.dumps(self.find())

    def test_five_at_most_and_the_list_keeps_their_order(self):
        paths = [self.file(f"Documents/tax-{n}.pdf", n) for n in range(8)]
        self.index(*reversed(paths))
        result = self.find()
        self.assertEqual(result["count"], 8)
        self.assertEqual([r["n"] for r in result["results"]], [1, 2, 3, 4, 5])
        self.assertEqual(self.session.found, [str(p) for p in paths[:5]])

    def test_the_same_hit_twice_counts_once(self):
        path = self.file("Documents/tax.pdf")
        self.index(path, path)
        self.assertEqual(self.find()["count"], 1)

    def test_a_year_matches_the_name_the_path_or_the_date(self):
        named = self.file("Documents/SA100-2024.pdf", 300)
        filed = self.file("Documents/2024/return.pdf", 300)
        dated = self.file("Documents/plain.pdf")
        when = time.mktime((2024, 6, 1, 12, 0, 0, 0, 0, -1))
        os.utime(dated, (when, when))
        other = self.file("Documents/other.pdf")
        self.index(named, filed, dated, other)
        for year in (2024, "2024"):
            self.assertEqual(set(self.names(year=year)),
                             {"SA100-2024.pdf", "return.pdf", "plain.pdf"})

    def test_a_build_stamp_is_not_a_year(self):
        self.file("go/mod@v0.0.0-20240729143630-8497ac4dab2e/tax.txt", age=0)
        wanted = files._Wanted(year=2024)
        path = self.home / "go/mod@v0.0.0-20240729143630-8497ac4dab2e/tax.txt"
        info = path.stat()
        with mock.patch.object(files.time, "localtime", return_value=time.struct_time(
                (2031, 1, 1, 0, 0, 0, 0, 1, 0))):
            self.assertFalse(wanted.info(path, info))

    def test_a_year_can_open_a_date_but_not_end_a_longer_number(self):
        self.index(self.file("Pictures/IMG_20240101.jpg"), self.file("Pictures/ref-12024.jpg"))
        with mock.patch("tuxistant.files.time.localtime", return_value=time.gmtime(0)):
            self.assertEqual(self.names(year=2024), ["IMG_20240101.jpg"])

    def test_the_year_in_a_query_is_lifted_out_of_it(self):
        self.index(self.file("Documents/SA100-2024.pdf"), self.file("Documents/SA100-2023.pdf"))
        self.assertEqual(self.names("sa100 2024"), ["SA100-2024.pdf"])
        self.searched.assert_called_with(["sa100"])

    def test_a_query_that_is_only_a_year_stays_a_query(self):
        self.index()
        self.find("2024")
        self.searched.assert_called_with(["2024"])

    def test_nonsense_in_year_is_ignored(self):
        self.index(self.file("Documents/tax.pdf"))
        self.assertEqual(self.find(year="soon")["count"], 1)

    def test_kind_goes_by_the_end_of_the_name(self):
        self.index(self.file("Documents/a.pdf"), self.file("Documents/b.jpg"),
                   self.file("Documents/c.mp4"), self.file("Documents/d.MP3"),
                   self.file("Documents/e.zip"))
        for kind, name in (("document", "a.pdf"), ("image", "b.jpg"), ("video", "c.mp4"),
                           ("audio", "d.MP3"), ("photos", "b.jpg"), ("music", "d.MP3")):
            with self.subTest(kind):
                self.assertEqual(self.names("thing", kind=kind), [name])

    def test_an_invented_kind_is_ignored(self):
        self.index(self.file("Documents/a.pdf"))
        self.assertEqual(self.find(kind="spreadsheety")["count"], 1)

    def test_folder_comes_in_many_forms(self):
        self.file("Documents/Tax/a.pdf")
        kept = self.file("Downloads/Tax/b.pdf")
        other = self.home / "Documents/Tax/a.pdf"
        self.index(kept, other)
        for folder in ("Downloads", "downloads", "~/Downloads", "my downloads folder",
                       "the Downloads folder", str(self.home / "Downloads"), "Downloads/Tax"):
            with self.subTest(folder):
                self.assertEqual(self.names(folder=folder), ["b.pdf"])

    def test_a_bare_folder_name_can_be_anywhere(self):
        kept = self.file("Documents/Work/Tax/a.pdf")
        self.index(kept, self.file("Documents/b.pdf"))
        self.assertEqual(self.names(folder="tax"), ["a.pdf"])

    def test_a_folder_outside_home_is_refused(self):
        self.index()
        with self.assertRaises(ValueError) as caught:
            self.find(folder="/etc")
        self.assert_spoken(str(caught.exception))

    def test_days_keeps_what_changed_lately(self):
        self.index(self.file("Documents/new.pdf", 2), self.file("Documents/old.pdf", 20))
        for days in (7, "7", "7 days", "1 week"):
            with self.subTest(days):
                self.assertEqual(self.names(days=days), ["new.pdf"])
        self.assertEqual(len(self.names(days="lately")), 2)

    def test_empty_optional_arguments_change_nothing(self):
        self.index(self.file("Documents/a.pdf"))
        self.assertEqual(self.find(kind="", year=0, folder="", days=0)["count"], 1)
        self.assertEqual(self.find(kind=None, year=None, folder="none", days=None)["count"], 1)

    def test_no_match_is_a_result_and_forgets_the_last_list(self):
        self.session.found = ["/old/list"]
        self.index()
        self.assertEqual(self.find(), {"count": 0, "results": []})
        self.assertEqual(self.session.found, [])

    def test_padding_never_reaches_the_index(self):
        self.index()
        self.find("my tax return from the files")
        self.searched.assert_called_with(["tax", "return"])

    def test_nothing_in_a_query_can_be_a_switch_or_search_syntax(self):
        self.index()
        self.find('--limit "tax*" (return)')
        self.searched.assert_called_with(["limit", "tax", "return"])

    def test_a_long_query_is_cut(self):
        self.index()
        self.find(" ".join(f"word{n}" for n in range(20)))
        self.assertEqual(len(self.searched.call_args[0][0]), files.MAX_QUERY_WORDS)

    def test_no_words_lists_what_is_in_the_folder(self):
        self.index()
        self.file("Downloads/.hidden.pdf")
        self.file("Downloads/.private/a.pdf")
        self.file("Downloads/old.pdf", 20)
        self.file("Downloads/sub/new.pdf", 1)
        self.assertEqual(self.names("files", folder="Downloads", days=7), ["new.pdf"])
        self.searched.assert_not_called()

    def test_no_words_with_a_kind_looks_in_the_usual_folders(self):
        self.index()
        self.file("Pictures/a.jpg")
        self.file("Projects/b.jpg")
        self.assertEqual(self.names("", kind="image"), ["a.jpg"])

    def test_nothing_at_all_to_go_on_asks(self):
        self.index()
        for query in ("", "the file", None):
            with self.subTest(query), self.assertRaises(ValueError) as caught:
                files.find({"query": query}, None, self.session)
            self.assert_spoken(str(caught.exception))

    def test_a_hostile_name_reaches_the_model_in_plain_sight(self):
        path = self.file("Documents/tax‮\nfdp.exe")
        self.index(path)
        name = self.find()["results"][0]["name"]
        self.assertNotIn("\n", name)
        self.assertNotIn("‮", name)
        self.assertEqual(self.session.found, [str(path)])

    def test_the_xdg_config_can_move_a_folder(self):
        self.file(".config/user-dirs.dirs").write_text('XDG_DOCUMENTS_DIR="$HOME/Dokumente"\n')
        self.index(self.file("Dokumente/tax.pdf", 9), self.file("Other/tax-new.pdf"))
        self.assertEqual(self.names(), ["tax.pdf", "tax-new.pdf"])


def answer(stdout=b"", code=0, stderr=b""):
    return subprocess.CompletedProcess([], code, stdout, stderr)


class BackEnds(Home):
    def test_localsearch_output_is_decoded(self):
        output = (b"Results:\n"
                  b"file:///home/me/My%20Tax/SA100%E2%82%AC.pdf\n"
                  b"\x1b[32mfile:///home/me/a.pdf\x1b[0m\n"
                  b"  a snippet of text\n\n"
                  b"file:///home/me/not-utf8-%FF.pdf\n")
        with mock.patch("subprocess.run", return_value=answer(output)):
            found = files._localsearch(["tax"])
        self.assertEqual(found[:2], [Path("/home/me/My Tax/SA100€.pdf"), Path("/home/me/a.pdf")])
        self.assertEqual(os.fsencode(found[2]), b"/home/me/not-utf8-\xff.pdf")

    def test_plocate_output_is_split_on_nothing_a_name_can_hold(self):
        with mock.patch("subprocess.run", return_value=answer(b"/home/me/a\nb\0/home/me/c\0")):
            self.assertEqual(files._plocate(["a"]), [Path("/home/me/a\nb"), Path("/home/me/c")])

    def test_both_use_the_words_after_a_double_dash_and_a_timeout(self):
        for search in (files._localsearch, files._plocate):
            with self.subTest(search), mock.patch("subprocess.run",
                                                  return_value=answer()) as run:
                search(["tax", "return"])
                command = run.call_args[0][0]
                self.assertEqual(command[-3:], ["--", "tax", "return"])
                self.assertEqual(run.call_args[1]["timeout"], files.SEARCH_TIMEOUT_S)

    def test_plocate_finding_nothing_is_not_a_failure(self):
        with mock.patch("subprocess.run", return_value=answer(code=1)):
            self.assertEqual(files._plocate(["a"]), [])

    def test_failures_are_none(self):
        for outcome in ({"return_value": answer(code=1, stderr=b"no database")},
                        {"side_effect": FileNotFoundError("plocate")},
                        {"side_effect": subprocess.TimeoutExpired("plocate", 8)}):
            with self.subTest(outcome), mock.patch("subprocess.run", **outcome):
                self.assertIsNone(files._plocate(["a"]))
                self.assertIsNone(files._localsearch(["a"]))

    def find(self, index, locate):
        with mock.patch.object(files, "_localsearch", return_value=index), \
                mock.patch.object(files, "_plocate", return_value=locate):
            return files.find({"query": "tax"}, None, self.session)

    def test_plocate_steps_in_when_the_index_fails(self):
        self.assertEqual(self.find(None, [self.file("Documents/tax.pdf")])["count"], 1)

    def test_plocate_steps_in_when_the_index_has_nothing_usable(self):
        hidden = self.file(".cache/tax.pdf")
        self.assertEqual(self.find([hidden], [self.file("Documents/tax.pdf")])["count"], 1)

    def test_plocate_failing_after_an_empty_index_is_still_no_match(self):
        self.assertEqual(self.find([], None), {"count": 0, "results": []})

    def test_both_failing_is_said_as_a_failure(self):
        with self.assertRaises(RuntimeError) as caught:
            self.find(None, None)
        self.assert_spoken(str(caught.exception))


class Resolving(Home):
    def setUp(self):
        super().setUp()
        self.first = self.file("Documents/Tax/SA100-2024.pdf")
        self.second = self.file("Downloads/Invoice-0010.pdf")
        self.third = self.file("Downloads/Receipt.txt")
        self.session.found = [str(self.first), str(self.second), str(self.third)]

    def refused(self, value):
        with self.assertRaises(ValueError) as caught:
            files.resolve(value, self.session)
        self.assert_spoken(str(caught.exception))
        return str(caught.exception)

    def test_result_numbers(self):
        for value, path in ((1, self.first), ("2", self.second), (3.0, self.third),
                            (" 3 ", self.third), ("#2", self.second), ("number 2", self.second),
                            ("result 1", self.first), ("file 3", self.third)):
            with self.subTest(value):
                self.assertEqual(files.resolve(value, self.session), path)

    def test_ordinal_words(self):
        for value, path in (("first", self.first), ("the first one", self.first),
                            ("second one", self.second), ("Third", self.third),
                            ("the 2nd", self.second), ("the second file", self.second),
                            ("last", self.third), ("the last one", self.third),
                            ("one", self.first), ("two", self.second)):
            with self.subTest(value):
                self.assertEqual(files.resolve(value, self.session), path)

    def test_a_number_with_no_result_behind_it(self):
        for value in (0, 4, "9", "fifth", -1, 2024):
            with self.subTest(value):
                self.assertIn("3 results", self.refused(value))

    def test_a_number_with_no_list_yet(self):
        self.session.found = []
        for value in (1, "first", "last"):
            with self.subTest(value):
                self.assertIn("Search", self.refused(value))

    def test_names_from_the_list(self):
        for value, path in (("SA100-2024.pdf", self.first), ("sa100-2024.PDF", self.first),
                            ("SA100-2024", self.first), ("invoice", self.second),
                            ("0010.pdf", self.second)):
            with self.subTest(value):
                self.assertEqual(files.resolve(value, self.session), path)

    def test_a_name_on_the_list_twice_needs_a_number(self):
        twin = self.file("Desktop/Invoice-0010.pdf")
        self.session.found.append(str(twin))
        self.assertIn("number", self.refused("Invoice-0010.pdf"))

    def test_paths_under_home(self):
        for value in ("~/Documents/Tax/SA100-2024.pdf", "Documents/Tax/SA100-2024.pdf",
                      str(self.first), f"file://{self.first}", "~/documents/tax/sa100-2024.pdf",
                      "~/Downloads/../Documents/Tax/SA100-2024.pdf"):
            with self.subTest(value):
                self.session.found = []
                self.assertEqual(files.resolve(value, self.session), self.first)

    def test_a_path_with_a_percent_sign_in_a_file_uri(self):
        path = self.file("Documents/My Letter.pdf")
        uri = "file://" + str(path).replace(" ", "%20")
        self.assertEqual(files.resolve(uri, self.session), path)

    def test_a_scrap_of_a_name_picks_nothing(self):
        for value in ("i", "pd", ".", "t"):
            with self.subTest(value):
                self.assertNotIn("number", self.refused(value))

    def test_nothing_to_go_on(self):
        for value in (None, "", "   ", True, False, [], {}, ["1"], 1.5, "a\0b"):
            with self.subTest(value):
                self.refused(value)

    def test_nothing_of_that_name(self):
        for value in ("missing.pdf", "~/Documents/missing.pdf", "Documents/Tax/nope/deep.pdf",
                      "-1"):
            with self.subTest(value):
                self.assertIn("can't find", self.refused(value))

    def test_outside_home(self):
        secret = self.file("secret.txt", base=self.outside)
        for value in ("/etc/passwd", str(secret), "~/../outside/secret.txt",
                      "../outside/secret.txt", f"file://{secret}", "/"):
            with self.subTest(value):
                self.assertIn("home folder", self.refused(value))

    def test_the_home_folder_is_not_inside_itself(self):
        for value in ("~", "~/", str(self.home), "."):
            with self.subTest(value):
                self.assertIn("home folder", self.refused(value))

    def test_another_users_home_does_not_expand(self):
        self.assertIn("can't find", self.refused("~root/notes.txt"))

    def test_hidden_things(self):
        self.file(".ssh/id_rsa")
        self.file(".bashrc")
        self.file("Documents/.private/a.pdf")
        for value in ("~/.ssh/id_rsa", ".ssh/id_rsa", "~/.bashrc", str(self.home / ".ssh/id_rsa"),
                      "~/Documents/.private/a.pdf", "~/.ssh", "~/.not/there"):
            with self.subTest(value):
                self.assertIn("hidden", self.refused(value))

    def test_a_link_cannot_lead_out_of_home(self):
        self.file("secret.txt", base=self.outside)
        self.link("Documents/a-link.txt", self.outside / "secret.txt")
        self.link("Documents/a-folder", self.outside)
        for value in ("~/Documents/a-link.txt", "~/Documents/a-folder/secret.txt",
                      "~/Documents/a-folder"):
            with self.subTest(value):
                self.assertIn("home folder", self.refused(value))

    def test_a_link_cannot_lead_into_a_hidden_folder(self):
        target = self.file(".ssh/id_rsa")
        self.link("Documents/key", target)
        self.link("Documents/ssh", target.parent)
        for value in ("~/Documents/key", "~/Documents/ssh/id_rsa"):
            with self.subTest(value):
                self.assertIn("hidden", self.refused(value))

    def test_a_link_in_a_hidden_folder_is_hidden_too(self):
        self.link(".stuff/report", self.first)
        self.assertIn("hidden", self.refused("~/.stuff/report"))

    def test_a_link_inside_home_gives_the_real_file(self):
        self.link("Desktop/shortcut.pdf", self.first)
        self.assertEqual(files.resolve("~/Desktop/shortcut.pdf", self.session), self.first)

    def test_broken_and_circular_links(self):
        self.link("Documents/broken", self.home / "nothing")
        loop = self.home / "Documents/loop"
        loop.symlink_to(loop)
        for value in ("~/Documents/broken", "~/Documents/loop"):
            with self.subTest(value):
                self.assertIn("can't find", self.refused(value))

    def test_the_list_gets_no_special_trust(self):
        self.session.found = ["/etc/passwd", str(self.home / ".ssh/id"), str(self.home / "gone")]
        self.assertIn("home folder", self.refused(1))
        self.assertIn("hidden", self.refused(2))
        self.assertIn("can't find", self.refused(3))

    def test_a_home_folder_reached_through_a_link_still_works(self):
        alias = self.root / "alias"
        alias.symlink_to(self.home)
        with mock.patch.dict(os.environ, {"HOME": str(alias)}):
            for value in ("~/Downloads/Receipt.txt", str(alias / "Downloads/Receipt.txt")):
                self.assertEqual(files.resolve(value, Session()), self.third)


class Opening(Home):
    def setUp(self):
        super().setUp()
        self.opened = []
        patcher = mock.patch.object(files, "_launch", self.gio_open)
        patcher.start()
        self.addCleanup(patcher.stop)

    def gio_open(self, path):
        """Stands in for the one place that opens anything, as a real one reaches the desktop."""
        self.opened.append(["gio", "open", "--", str(path)])
        return True

    def open(self, value):
        return files.open_file({"file": value}, None, self.session)

    @unittest.skipUnless(shutil.which("gio"), "needs gio")
    def test_documents_open(self):
        for name in ("report.pdf", "notes.txt", "notes.md", "table.csv", "log.log", "photo.jpg",
                     "song.mp3", "film.mp4", "letter.odt", "letter.docx", "sheet.xlsx",
                     "old.doc", "book.epub", "data.json"):
            with self.subTest(name):
                path = self.file(f"Documents/{name}")
                self.assertEqual(self.open(f"~/Documents/{name}"), {"opened": name})
                self.assertEqual(self.opened.pop(), ["gio", "open", "--", str(path)])

    def test_a_folder_opens(self):
        self.assertEqual(self.open("~/Documents"), {"opened": "Documents"})

    @unittest.skipUnless(shutil.which("gio"), "needs gio")
    def test_anything_that_could_run_stays_closed(self):
        for name in ("run.sh", "tool.py", "app.desktop", "tool.pl", "tool.rb", "page.js",
                     "app.AppImage", "installer.run", "firmware.bin", "app.jar", "setup.exe",
                     "setup.msi", "go.bat", "go.cmd", "go.ps1", "go.vbs", "app.deb", "app.rpm",
                     "app.apk", "app.snap", "app.flatpakref", "page.html", "page.xhtml",
                     "logo.svg", "macro.docm", "macro.xlsm", "shortcut.lnk", "link.url",
                     "disk.iso", "archive.zip"):
            with self.subTest(name):
                self.file(f"Documents/{name}")
                with self.assertRaises(ValueError) as caught:
                    self.open(f"~/Documents/{name}")
                self.assert_spoken(str(caught.exception))
                self.assertEqual(self.opened, [])

    @unittest.skipUnless(shutil.which("gio"), "needs gio")
    def test_what_is_inside_counts_when_the_name_says_nothing(self):
        for name, body in (("script", b"#!/bin/sh\necho hi\n"),
                           ("program", b"\x7fELF\x02\x01\x01" + bytes(9))):
            with self.subTest(name):
                self.file(f"Documents/{name}").write_bytes(body)
                with self.assertRaises(ValueError):
                    self.open(f"~/Documents/{name}")
        self.assertEqual(self.opened, [])

    def test_the_execute_bit_is_enough(self):
        path = self.file("Documents/report.pdf")
        path.chmod(0o755)
        with self.assertRaises(ValueError):
            self.open("~/Documents/report.pdf")
        self.assertEqual(self.opened, [])

    def test_a_type_nobody_can_tell_stays_closed(self):
        self.file("Documents/report.pdf")
        with mock.patch.object(files, "_content_type", return_value=""), \
                self.assertRaises(ValueError):
            self.open("~/Documents/report.pdf")
        self.assertEqual(self.opened, [])

    def test_a_name_cannot_forge_the_content_type(self):
        output = (b"uri: file:///x\nlocal path: /x\n  standard::content-type: application/pdf\n"
                  b"attributes:\n  standard::content-type: application/x-shellscript\n")
        with mock.patch.object(files, "_run", return_value=answer(output)):
            self.assertEqual(files._content_type(Path("/x")), "application/x-shellscript")

    def test_it_goes_through_resolve(self):
        self.file("secret.pdf", base=self.outside)
        self.link("Documents/away.pdf", self.outside / "secret.pdf")
        for value in ("/etc/passwd", "~/Documents/away.pdf", "~/.ssh/id_rsa", 1, None):
            with self.subTest(value), self.assertRaises(ValueError):
                self.open(value)
        self.assertEqual(self.opened, [])

    def test_a_result_number_opens(self):
        path = self.file("Documents/report.pdf")
        self.session.found = [str(path)]
        with mock.patch.object(files, "_content_type", return_value="application/pdf"):
            self.assertEqual(self.open(1), {"opened": "report.pdf"})
            self.assertEqual(self.open("first"), {"opened": "report.pdf"})

    def test_a_link_opens_as_what_it_points_at(self):
        path = self.file("Documents/report.pdf")
        self.link("Desktop/report.pdf", path)
        with mock.patch.object(files, "_content_type", return_value="application/pdf"):
            self.open("~/Desktop/report.pdf")
        self.assertEqual(self.opened[0][-1], str(path))

    def test_gio_failing_is_said_plainly(self):
        self.file("Documents/report.pdf")
        with mock.patch.object(files, "_content_type", return_value="application/pdf"), \
                mock.patch.object(files, "_launch", return_value=False), \
                self.assertRaises(RuntimeError) as caught:
            self.open("~/Documents/report.pdf")
        self.assert_spoken(str(caught.exception))


class Trashing(Home):
    def setUp(self):
        super().setUp()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.batches = []
        self.stuck = set()
        patcher = mock.patch.object(files, "_run", self.gio)
        patcher.start()
        self.addCleanup(patcher.stop)

    def gio(self, command, timeout):
        """Stands in for gio trash, leaving the real Trash out of it."""
        self.assertEqual(command[:3], ["gio", "trash", "--"])
        self.assertGreater(timeout, 0)
        self.batches.append(command[3:])
        for path in command[3:]:
            if path not in self.stuck:
                os.rename(path, self.bin / str(len(os.listdir(self.bin))))
        return answer()

    def trash(self, **args):
        return files.trash(args, None, self.session)

    def card(self, **args) -> Ask:
        ask = self.trash(**args)
        self.assertIsInstance(ask, Ask)
        self.assertEqual(self.batches, [], "nothing may move before the click")
        return ask

    def refused(self, **args) -> str:
        with self.assertRaises(ValueError) as caught:
            self.trash(**args)
        self.assert_spoken(str(caught.exception))
        self.assertEqual(self.batches, [])
        return str(caught.exception)

    def moved(self) -> list[str]:
        return [path for batch in self.batches for path in batch]

    def test_one_file(self):
        path = self.file("Downloads/old.pdf", size=1_234_567)
        ask = self.card(file="~/Downloads/old.pdf")
        self.assertEqual(ask.title, "Move 1 file (1.2 MB) to the Trash?")
        self.assertEqual(ask.detail, "~/Downloads/old.pdf")
        self.assertEqual(ask.level, "click")
        self.assertTrue(path.exists())
        self.assertEqual(ask.run(), {"moved": 1, "failed": 0})
        self.assertFalse(path.exists())

    def test_a_list_of_references(self):
        paths = [self.file(f"Downloads/{n}.pdf", size=1000) for n in range(3)]
        self.session.found = [str(p) for p in paths]
        ask = self.card(file=[1, "the last one", "~/Downloads/1.pdf"])
        self.assertEqual(ask.title, "Move 3 files (3.0 KB) to the Trash?")
        self.assertEqual(ask.detail.split("\n"), ["~/Downloads/0.pdf", "~/Downloads/2.pdf",
                                                  "~/Downloads/1.pdf"])
        self.assertEqual(ask.run(), {"moved": 3, "failed": 0})

    def test_the_same_file_twice_is_one_file(self):
        path = self.file("Downloads/a.pdf")
        self.session.found = [str(path)]
        ask = self.card(file=[1, "first", "~/Downloads/a.pdf"])
        self.assertEqual(ask.run(), {"moved": 1, "failed": 0})

    def test_one_bad_reference_stops_the_whole_list(self):
        keep = self.file("Downloads/a.pdf")
        self.refused(file=["~/Downloads/a.pdf", "/etc/passwd"])
        self.assertTrue(keep.exists())

    def test_a_folder_is_swept_recursively(self):
        top = self.file("Downloads/a.pdf")
        deep = self.file("Downloads/x/y/b.pdf")
        ask = self.card(folder="Downloads")
        self.assertEqual(ask.title, "Move 2 files (20 bytes) to the Trash?")
        ask.run()
        self.assertEqual(sorted(self.moved()), sorted([str(top), str(deep)]))

    def test_a_sweep_skips_dot_names_and_links(self):
        self.file("Downloads/.hidden")
        self.file("Downloads/.private/a.pdf")
        target = self.file("Documents/real.pdf")
        (self.home / "Downloads/file-link").symlink_to(target)
        (self.home / "Downloads/folder-link").symlink_to(self.home / "Documents")
        self.file("secret.txt", base=self.outside)
        (self.home / "Downloads/away").symlink_to(self.outside)
        os.mkfifo(self.home / "Downloads/pipe")
        kept = self.file("Downloads/keep.pdf")
        ask = self.card(folder="Downloads")
        ask.run()
        self.assertEqual(self.moved(), [str(kept)])

    def test_older_than_days(self):
        old = self.file("Downloads/old.pdf", 40)
        self.file("Downloads/new.pdf", 10)
        for age in (30, "30", "30 days", "1 month", 30.0):
            with self.subTest(age):
                ask = self.card(folder="Downloads", older_than_days=age)
                self.assertIn("1 file ", ask.title)
                self.assertEqual(ask.detail, "~/Downloads/old.pdf")
        self.assertEqual(old.exists(), True)

    def test_nonsense_in_older_than_days_never_widens_the_list(self):
        self.file("Downloads/new.pdf")
        for age in ("soon", "-5", -5, "a while", True):
            with self.subTest(age):
                self.assertIn("days", self.refused(folder="Downloads", older_than_days=age))

    def test_an_empty_older_than_days_means_none_was_given(self):
        self.file("Downloads/new.pdf")
        self.assertIn("1 file ", self.card(folder="Downloads", older_than_days=0).title)

    def test_nothing_matched_is_a_plain_result(self):
        self.file("Downloads/new.pdf")
        result = self.trash(folder="Downloads", older_than_days=30)
        self.assertEqual((result["moved"], result["failed"]), (0, 0))
        self.assert_spoken(result["note"])
        self.assertEqual(self.batches, [])

    def test_the_card_lists_fifteen_and_counts_the_rest(self):
        for n in range(20):
            self.file(f"Downloads/{n:02}.pdf")
        ask = self.card(folder="Downloads")
        lines = ask.detail.split("\n")
        self.assertEqual(len(lines), 16)
        self.assertEqual(lines[-1], "and 5 more")
        self.assertEqual(ask.title, "Move 20 files (200 bytes) to the Trash?")
        self.assertEqual(ask.run(), {"moved": 20, "failed": 0})
        self.assertEqual(len(self.moved()), 20)

    def test_long_paths_cannot_push_the_count_off_the_card(self):
        for n in range(20):
            self.file(f"Downloads/{'x' * 200}/{'y' * 200}/{n:02}.pdf")
        detail = self.card(folder="Downloads").detail
        self.assertTrue(detail.endswith("and 5 more"))
        self.assertEqual(printable(detail, MAX_DETAIL_CHARS), detail)

    def test_a_name_cannot_fake_a_line_on_the_card(self):
        self.file("Downloads/a\nand 0 more‮.pdf")
        detail = self.card(folder="Downloads").detail
        self.assertEqual(detail.count("\n"), 0)
        self.assertNotIn("‮", printable(detail, MAX_DETAIL_CHARS))

    def test_big_jobs_go_in_batches_and_get_more_time(self):
        for n in range(250):
            self.file(f"Downloads/{n:03}.pdf")
        ask = self.card(folder="Downloads")
        self.assertGreater(ask.timeout_s, 30)
        self.assertEqual(ask.run(), {"moved": 250, "failed": 0})
        self.assertEqual([len(batch) for batch in self.batches], [100, 100, 50])

    def test_too_many_files_is_refused(self):
        for n in range(4):
            self.file(f"Downloads/sub/{n}.pdf")
        with mock.patch.object(files, "MAX_TRASHED", 3):
            self.assertIn("too many", self.refused(folder="Downloads"))
            self.assertIn("too many", self.refused(file="~/Downloads/sub"))

    def test_run_moves_the_list_not_what_is_there_now(self):
        self.file("Downloads/a.pdf")
        ask = self.card(folder="Downloads")
        late = self.file("Downloads/b.pdf")
        ask.run()
        self.assertEqual(self.moved(), [str(self.home / "Downloads/a.pdf")])
        self.assertTrue(late.exists())

    def test_a_file_swapped_after_the_card_stays(self):
        path = self.file("Downloads/a.pdf")
        ask = self.card(folder="Downloads")
        path.rename(self.home / "Downloads/moved-away.pdf")
        self.file("Downloads/a.pdf", size=999)
        self.assertEqual(ask.run(), {"moved": 0, "failed": 1})
        self.assertEqual(self.batches, [])
        self.assertTrue(path.exists())

    def test_a_file_that_went_away_counts_as_failed(self):
        path = self.file("Downloads/a.pdf")
        ask = self.card(file="~/Downloads/a.pdf")
        path.rename(self.bin / "elsewhere")
        self.assertEqual(ask.run(), {"moved": 0, "failed": 1})

    def test_what_gio_leaves_behind_counts_as_failed(self):
        stuck = self.file("Downloads/a.pdf")
        self.file("Downloads/b.pdf")
        self.stuck.add(str(stuck))
        self.assertEqual(self.card(folder="Downloads").run(), {"moved": 1, "failed": 1})

    def test_no_gio_moves_nothing(self):
        self.file("Downloads/a.pdf")
        ask = self.card(folder="Downloads")
        with mock.patch.object(files, "_run", return_value=None):
            self.assertEqual(ask.run(), {"moved": 0, "failed": 1})

    def test_out_of_time_stops_and_says_so(self):
        path = self.file("Downloads/a.pdf")
        target = files._Target(path, files._identity(path), 10)
        self.assertEqual(files._move([target], 0), {"moved": 0, "failed": 1})
        self.assertEqual(self.batches, [])

    def test_the_home_folder_is_refused(self):
        for args in ({"file": "~"}, {"folder": "~"}, {"folder": "home"},
                     {"folder": "my home folder"}, {"file": str(self.home)},
                     {"folder": str(self.home)}):
            with self.subTest(args):
                self.assertIn("home folder", self.refused(**args))

    def test_the_big_folders_cannot_be_named_as_a_file(self):
        for value in ("~/Documents", "Downloads", "~/Desktop/", "~/Music", "~/pictures"):
            with self.subTest(value):
                self.assertIn("whole", self.refused(file=value))

    def test_a_moved_big_folder_is_still_big(self):
        self.file(".config/user-dirs.dirs").write_text('XDG_DOCUMENTS_DIR="$HOME/Dokumente"\n')
        (self.home / "Dokumente").mkdir()
        self.assertIn("whole", self.refused(file="~/Dokumente"))

    def test_a_folder_holding_a_big_folder_is_refused(self):
        self.file(".config/user-dirs.dirs").write_text('XDG_DOCUMENTS_DIR="$HOME/stuff/Docs"\n')
        (self.home / "stuff/Docs").mkdir(parents=True)
        self.assertIn("whole", self.refused(file="~/stuff"))

    def test_a_link_to_a_big_folder_moves_only_the_link(self):
        link = self.link("Desktop/docs", self.home / "Documents")
        ask = self.card(file="~/Desktop/docs")
        self.assertEqual(ask.detail, "~/Desktop/docs")
        ask.run()
        self.assertEqual(self.moved(), [str(link)])

    def test_hidden_and_outside_are_refused(self):
        self.file(".ssh/id_rsa")
        self.file("secret.txt", base=self.outside)
        self.link("Downloads/away", self.outside)
        for args in ({"file": "~/.ssh/id_rsa"}, {"file": "/etc/passwd"}, {"folder": "/etc"},
                     {"folder": "~/.ssh"}, {"folder": str(self.outside)},
                     {"file": "~/Downloads/away/secret.txt"}, {"file": "~/Downloads/away"},
                     {"folder": "~/Downloads/away"}, {"file": "~/../outside/secret.txt"}):
            with self.subTest(args):
                self.refused(**args)

    def test_a_link_moves_as_a_link(self):
        real = self.file("Documents/real.pdf")
        link = self.link("Desktop/shortcut.pdf", real)
        ask = self.card(file="~/Desktop/shortcut.pdf")
        self.assertEqual(ask.detail, "~/Desktop/shortcut.pdf")
        ask.run()
        self.assertEqual(self.moved(), [str(link)])
        self.assertTrue(real.exists())

    def test_a_folder_goes_whole_and_the_card_counts_what_is_in_it(self):
        self.file("Projects/old/a.txt", size=2000)
        self.file("Projects/old/deep/b.txt", size=3000)
        self.file("Projects/old/.git/config", size=1000)
        ask = self.card(file="~/Projects/old")
        self.assertEqual(ask.title, "Move 1 folder (3 files in all, 6.0 KB) to the Trash?")
        self.assertEqual(ask.detail, "~/Projects/old/ (3 files, 6.0 KB)")
        self.assertEqual(ask.run(), {"moved": 1, "failed": 0})
        self.assertEqual(self.moved(), [str(self.home / "Projects/old")])

    def test_files_and_folders_together(self):
        self.file("Projects/old/a.txt", size=1000)
        self.file("Downloads/b.txt", size=1000)
        ask = self.card(file=["~/Projects/old", "~/Downloads/b.txt"])
        self.assertEqual(ask.title,
                         "Move 1 file and 1 folder (2 files in all, 2.0 KB) to the Trash?")

    def test_something_that_is_not_a_file_stays(self):
        os.mkfifo(self.home / "Downloads/pipe")
        self.assertIn("ordinary", self.refused(file="~/Downloads/pipe"))

    def test_a_folder_argument_must_be_a_folder(self):
        self.file("Downloads/a.pdf")
        self.assertIn("not a folder", self.refused(folder="~/Downloads/a.pdf"))

    def test_nothing_named(self):
        for args in ({}, {"file": ""}, {"file": []}, {"folder": ""}, {"older_than_days": 30},
                     {"file": None, "folder": None}):
            with self.subTest(args):
                self.assertIn("which", self.refused(**args))

    def test_file_wins_over_folder(self):
        self.file("Downloads/a.pdf")
        self.file("Downloads/b.pdf")
        self.assertIn("1 file ", self.card(file="~/Downloads/a.pdf", folder="Downloads").title)

    def test_model_wording_stays_off_the_card(self):
        self.file("Downloads/a.pdf")
        ask = self.card(folder="Downloads", title="Harmless tidy-up", detail="nothing to see")
        self.assertNotIn("Harmless", ask.title + ask.detail)

    def test_nothing_here_can_delete(self):
        source = Path(files.__file__).read_text()
        for word in ("unlink", "rmtree", "os.remove", "rmdir", '"rm"'):
            with self.subTest(word):
                self.assertNotIn(word, source)


@unittest.skipUnless(shutil.which("gio"), "needs gio")
class RealTrash(Home):
    """gio itself, with HOME and XDG_DATA_HOME pointing at the made-up home."""

    def setUp(self):
        super().setUp()
        self.trash_can = self.home / ".local/share/Trash/files"

    def test_a_file_lands_in_the_trash_not_nowhere(self):
        path = self.file("Downloads/old file.pdf")
        ask = files.trash({"file": "~/Downloads/old file.pdf"}, None, self.session)
        self.assertEqual(ask.run(), {"moved": 1, "failed": 0})
        self.assertFalse(path.exists())
        self.assertTrue((self.trash_can / "old file.pdf").exists())

    def test_a_link_goes_and_its_target_stays(self):
        real = self.file("Documents/real.pdf")
        link = self.link("Desktop/shortcut.pdf", real)
        ask = files.trash({"file": "~/Desktop/shortcut.pdf"}, None, self.session)
        self.assertEqual(ask.run(), {"moved": 1, "failed": 0})
        self.assertFalse(os.path.lexists(link))
        self.assertTrue(real.exists())

    def test_a_folder_goes_in_one_piece(self):
        self.file("Projects/old/.git/config")
        self.file("Projects/old/a.txt")
        ask = files.trash({"file": "~/Projects/old"}, None, self.session)
        self.assertEqual(ask.run(), {"moved": 1, "failed": 0})
        self.assertTrue((self.trash_can / "old/.git/config").exists())

    def test_a_sweep_in_batches(self):
        for n in range(7):
            self.file(f"Downloads/{n}.pdf", 40)
        self.file("Downloads/.keep")
        ask = files.trash({"folder": "Downloads", "older_than_days": 30}, None, self.session)
        with mock.patch.object(files, "TRASH_BATCH", 3):
            self.assertEqual(ask.run(), {"moved": 7, "failed": 0})
        self.assertEqual(os.listdir(self.home / "Downloads"), [".keep"])

    def test_a_file_gio_cannot_move_is_counted(self):
        path = self.file("Downloads/locked/a.pdf")
        free = self.file("Downloads/b.pdf")
        ask = files.trash({"folder": "Downloads"}, None, self.session)
        path.parent.chmod(0o555)
        self.addCleanup(path.parent.chmod, 0o755)
        self.assertEqual(ask.run(), {"moved": 1, "failed": 1})
        self.assertTrue(path.exists())
        self.assertFalse(free.exists())


class Manifests(Home):
    def test_find_and_open_read_and_trash_writes(self):
        built = Skills(BUILTIN)
        for name, risk in (("file.find", "medium"), ("file.open", "medium"),
                           ("file.trash", "high")):
            with self.subTest(name):
                self.assertEqual(built.actions[name]["risk"], risk)
        self.assertEqual(built.actions["file.find"]["permissions"], {"files_read": ["~"]})
        self.assertEqual(built.actions["file.trash"]["permissions"], {"files_write": ["~"]})

    def test_every_action_has_a_handler(self):
        for name in ("files.yaml", "trash.yaml"):
            actions = yaml.safe_load((BUILTIN / name).read_text())["actions"]
            self.assertLessEqual(set(actions), set(files.HANDLERS))

    def test_descriptions_stay_short(self):
        for name in ("files.yaml", "trash.yaml"):
            for action in yaml.safe_load((BUILTIN / name).read_text())["actions"].values():
                self.assertLess(len(action["description"]), 200)


class ThroughTheGate(MadeUpHome, unittest.IsolatedAsyncioTestCase):
    """The same actions as the model reaches them."""

    def setUp(self):
        super().setUp()
        self.skills = Skills(BUILTIN)
        self.moves = []
        patcher = mock.patch.object(files, "_run", self.gio)
        patcher.start()
        self.addCleanup(patcher.stop)

    def gio(self, command, _timeout):
        self.moves.append(command)
        return answer()

    async def click(self, answer_with):
        self.asked = []

        async def confirm(ask):
            self.asked.append(ask)
            return answer_with
        return Session(self.skills, confirm)

    async def test_a_no_moves_nothing(self):
        path = self.file("Downloads/a.pdf")
        session = await self.click(False)
        arguments = '{"file": ["~/Downloads/a.pdf"]}'
        _, result = await self.skills.execute("file_trash", arguments, session)
        self.assertTrue(result["declined"])
        self.assertEqual(self.moves, [])
        self.assertTrue(path.exists())
        self.assertEqual(len(self.asked), 1)

    async def test_nobody_to_ask_is_a_no(self):
        self.file("Downloads/a.pdf")
        _, result = await self.skills.execute("file_trash", '{"folder": "Downloads"}',
                                              Session(self.skills))
        self.assertTrue(result["declined"])
        self.assertEqual(self.moves, [])

    async def test_a_yes_runs_gio_trash(self):
        path = self.file("Downloads/a.pdf")
        session = await self.click(True)
        await self.skills.execute("file_trash", '{"folder": "Downloads"}', session)
        self.assertEqual(self.moves, [["gio", "trash", "--", str(path)]])

    async def test_nothing_matched_needs_no_card(self):
        session = await self.click(True)
        _, result = await self.skills.execute("file_trash", '{"folder": "Downloads"}', session)
        self.assertEqual(result["moved"], 0)
        self.assertEqual(self.asked, [])

    async def test_a_refusal_is_a_sentence_for_the_model(self):
        session = await self.click(True)
        _, result = await self.skills.execute("file_trash", '{"file": "/etc/passwd"}', session)
        self.assert_spoken(result["error"])
        self.assertEqual(self.asked, [])

    async def test_a_find_taints_the_conversation_and_fills_the_list(self):
        path = self.file("Documents/tax.pdf")
        session = await self.click(True)
        with mock.patch.object(files, "_localsearch", return_value=[path]):
            _, result = await self.skills.execute("file_find", '{"query": "tax"}', session)
        self.assertEqual(result["count"], 1)
        self.assertEqual(session.found, [str(path)])
        self.assertTrue(session.tainted)

    async def test_find_then_trash_by_number(self):
        path = self.file("Documents/tax.pdf")
        session = await self.click(True)
        with mock.patch.object(files, "_localsearch", return_value=[path]):
            await self.skills.execute("file_find", '{"query": "tax"}', session)
        await self.skills.execute("file_trash", '{"file": [1]}', session)
        self.assertEqual(self.asked[0].detail, "~/Documents/tax.pdf")
        self.assertEqual(self.moves[0][-1], str(path))


class ReallyOpening(Home):
    """_launch for real, with a stand-in gio on the PATH: the real one would reach the desktop."""

    def gio(self, script: str):
        folder = self.root / "bin"
        folder.mkdir(exist_ok=True)
        (folder / "gio").write_text(f"#!/bin/sh\n{script}\n")
        (folder / "gio").chmod(0o755)
        patcher = mock.patch.dict(os.environ, {"PATH": f"{folder}:{os.environ['PATH']}"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_an_app_that_keeps_running_is_not_a_failure(self):
        # Most viewers are not D-Bus activated: gio exits at once and the app lives on, holding
        # whatever it inherited. A read of those pipes would wait until the app quit.
        self.gio("sleep 3 &\nexit 0")
        path = self.file("Documents/report.pdf")
        started = time.monotonic()
        self.assertTrue(files._launch(path))
        self.assertLess(time.monotonic() - started, 2)

    def test_gio_failing_is_a_failure(self):
        self.gio("exit 1")
        self.assertFalse(files._launch(self.file("Documents/report.pdf")))

    def test_no_gio_is_a_failure(self):
        with mock.patch.dict(os.environ, {"PATH": str(self.root)}):
            self.assertFalse(files._launch(self.file("Documents/report.pdf")))


class NamedOneByOne(Home):
    """The card has to show every file picked by name, or the one that matters can hide."""

    def test_every_named_file_is_on_the_card(self):
        names = [f"Downloads/tmp{i}.log" for i in range(files.MAX_LISTED - 1)]
        for name in names:
            self.file(name)
        self.file("Documents/thesis-final.odt")
        ask = files.trash({"file": [f"~/{n}" for n in names] + ["~/Documents/thesis-final.odt"]},
                          None, self.session)
        self.assertIn("thesis-final.odt", ask.detail)
        self.assertNotIn("more", ask.detail)

    def test_more_than_fit_on_the_card_is_refused(self):
        names = [f"Downloads/tmp{i}.log" for i in range(files.MAX_LISTED)]
        for name in names:
            self.file(name)
        self.file("Documents/thesis-final.odt")
        with self.assertRaises(ValueError) as caught:
            files.trash({"file": [f"~/{n}" for n in names] + ["~/Documents/thesis-final.odt"]},
                        None, self.session)
        self.assert_spoken(str(caught.exception))


if __name__ == "__main__":
    unittest.main()
