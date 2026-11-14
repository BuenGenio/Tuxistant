"""Mail search through GNOME Online Accounts, for people without Thunderbird.

Online Accounts is a made-up one on a dbus-daemon of this test's own, with no service files, so
the real one is never started or asked. The mail server is a made-up one on loopback, with a
certificate made for this run (tests/imap_fake.py). The model is test_mail's stub, which records
what it was sent.
"""
import asyncio
import base64
import email.message
import imaplib
import json
import logging
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tests import imap_fake  # noqa: E402
from tests.imap_fake import TOKEN, USER, Box, PrivateBus, Server, message  # noqa: E402
from tests.mail_index import build  # noqa: E402
from tests.test_mail import CONFIG, Base  # noqa: E402
from tuxistant import goamail, mail, research  # noqa: E402

OFFICE = "Citizen Services <no-reply@notify.citizen.example>"
PASSPORT = message("Passport application received", OFFICE,
                   "Thank you for sending your application to renew your passport. We will "
                   "write to you again when it has been printed.")
SCAM = message("Urgent: your application to renew your passport",
               "Passport Office <help@passport-support.example>",
               "Your application was rejected. Call us today and pay the fee.")
HELLO = message("Hello", OFFICE, "Hello.")
FOLDER = CERT = KEY = BUS = None


def setUpModule():
    global FOLDER, CERT, KEY, BUS
    if not (shutil.which("openssl") and shutil.which("dbus-daemon")):
        raise unittest.SkipTest("needs openssl and dbus-daemon")
    FOLDER = tempfile.TemporaryDirectory()
    CERT, KEY = imap_fake.certificate(Path(FOLDER.name))
    BUS = PrivateBus()


def tearDownModule():
    BUS.close()
    FOLDER.cleanup()


def gmail(inbox=(), sent=(), junk=(), archived=()) -> list[Box]:
    """Gmail's folders. Each mail is (raw, days ago the server took it in). Everything but Spam
    and Trash is in All Mail too, as on Gmail."""
    return [Box("INBOX", "\\HasNoChildren", inbox),
            Box("[Gmail]/All Mail", "\\HasNoChildren \\All", [*inbox, *sent, *archived]),
            Box("[Gmail]/Sent Mail", "\\HasNoChildren \\Sent", sent),
            Box("[Gmail]/Spam", "\\HasNoChildren \\Junk", junk),
            Box("[Gmail]/Trash", "\\HasNoChildren \\Trash", junk),
            Box("Junk", "\\HasNoChildren", junk)]


class Goa(Base):
    async def asyncSetUp(self):
        for target, name, value in ((goamail, "CA_FILE", str(CERT)),):
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.dict("os.environ", {"DBUS_SESSION_BUS_ADDRESS": BUS.address})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.calls = imap_fake.Calls()

    async def accounts(self, *specs):
        bus, self.calls = await imap_fake.online_accounts(BUS.address, list(specs))
        self.addAsyncCleanup(self._gone, bus)
        self.addAsyncCleanup(self._answered, self.calls)

    async def _answered(self, calls):
        calls.done.set()
        await asyncio.sleep(0.05)   # so that a token still on its way is answered, not cut off

    async def _gone(self, bus):
        bus.disconnect()
        await asyncio.sleep(0.05)   # so that the next test's Online Accounts can take the name

    async def mailbox(self, boxes, mode="ssl", account=None, **options) -> Server:
        server = Server(CERT, KEY, boxes, mode, **options)
        self.addCleanup(server.close)
        await self.accounts({"host": server.host, **(account or {})})
        return server

    def opened(self, server) -> list[str]:
        return [command.split(" ", 2)[2] for command in server.commands
                if command.split(" ")[1].upper() in ("EXAMINE", "SELECT")]


class Finding(Goa):
    async def test_without_thunderbird_the_account_is_searched_and_the_app_reads_the_answer(self):
        server = await self.mailbox(gmail(inbox=[(HELLO, 900), (PASSPORT, 13)]))
        self.says = ["You applied on Thursday."]
        said, told = await self.ask()
        self.assertEqual(told, research.TOLD)
        self.assertTrue(said.startswith("You applied on Thursday. That's from an email from "
                                        "notify.citizen.example on "), said)
        self.assertNotIn(mail.NEW_SENDER, said)
        self.assertIn("application to renew your passport", self.read(0))
        self.assertNotIn("tools", self.model[0][1])
        self.assertEqual(self.calls.made, ["GetAccessToken"])
        self.assertIn('"[Gmail]/All Mail"', self.opened(server))

    async def test_thunderbird_comes_first_and_then_no_account_is_asked(self):
        server = await self.mailbox(gmail(inbox=[(PASSPORT, 13)]))
        build(self.home, [{"subject": "Passport application received", "author": OFFICE,
                           "body": "Your application to renew your passport arrived."}])
        self.says = ["Then."]
        said, _ = await self.ask()
        self.assertTrue(said.startswith("Then."), said)
        self.assertEqual((self.calls.made, server.commands), ([], []))

    async def test_a_server_that_matches_whole_words_only_still_finds_it(self):
        """"passport" is looked for as "passp" too, which Gmail wouldn't find."""
        await self.mailbox(gmail(inbox=[(HELLO, 900), (PASSPORT, 13)]), whole_words=True)
        self.says = ["Then."]
        said, _ = await self.ask("When did my passport application arrive?")
        self.assertTrue(said.startswith("Then. That's from"), said)

    async def test_archived_mail_is_found_and_spam_trash_and_junk_are_never_opened(self):
        trap = message("Your passport renewal", "Shop <deals@shop.example>",
                       "Apply now to renew your passport in a day!")
        server = await self.mailbox(gmail(archived=[(HELLO, 900), (PASSPORT, 13)],
                                          junk=[(trap, 1)]))
        self.says = ["Then."]
        said, _ = await self.ask()
        self.assertTrue(said.startswith("Then."), said)
        self.assertEqual(len(self.model), 1)
        self.assertNotIn("Apply now", self.read(0))
        for box in self.opened(server):
            self.assertNotIn("Spam", box)
            self.assertNotIn("Trash", box)
            self.assertNotIn("Junk", box)

    async def test_without_all_mail_the_inbox_and_sent_are_searched(self):
        server = await self.mailbox([Box("INBOX", "", [(HELLO, 900), (PASSPORT, 13)]),
                                     Box("Trash/Sent", "", [(SCAM, 1)]),
                                     Box("Sent Items", ""), Box("Deleted Items", "", [(SCAM, 1)]),
                                     Box("Drafts", "\\Drafts")])
        self.says = ["Then."]
        said, _ = await self.ask()
        self.assertTrue(said.startswith("Then."), said)
        self.assertEqual(self.opened(server)[:2], ["INBOX", '"Sent Items"'])
        self.assertFalse([box for box in self.opened(server)
                          if "Deleted" in box or "Draft" in box or "Trash" in box])

    async def test_html_base64_and_encoded_subjects_are_read(self):
        html_only = message("=?utf-8?b?" + "UmVuZXcgeW91ciBwYXNzcG9ydA==" + "?=", OFFICE, None,
                            html="<html><head><style>p{}</style></head><body><p>Your passport "
                                 "renewal application <b>arrived</b> on Monday.</p></body></html>")
        await self.mailbox(gmail(inbox=[(HELLO, 900), (html_only, 13)]))
        await self.ask()
        self.assertIn("Subject: Renew your passport", self.read(0))
        self.assertIn("Your passport renewal application arrived on Monday.", self.read(0))
        self.assertNotIn("p{}", self.read(0))

    async def test_an_attachment_is_never_read(self):
        attached = email.message.EmailMessage()
        attached["From"], attached["Subject"] = OFFICE, "Passport renewal application"
        attached.set_content("<p>Your application arrived.</p>", subtype="html")
        attached.add_attachment("Passport renewal: the attached words.", filename="notes.txt")
        await self.mailbox(gmail(inbox=[(HELLO, 900), (attached.as_bytes(), 13)]))
        await self.ask()
        self.assertIn("Your application arrived.", self.read(0))
        self.assertNotIn("attached words", self.read(0))

    async def test_a_question_in_another_alphabet_is_found(self):
        concert = message("Билеты на концерт", f"Олена <olena@{'people.example'}>",
                          "Билеты на концерт у меня, начало в субботу в 19:30.")
        server = await self.mailbox(gmail(inbox=[(message("Привет", "olena@people.example"), 900),
                                                 (concert, 3)]))
        self.says = ["On Saturday at half past seven."]
        said, _ = await self.ask("Когда концерт?")
        self.assertTrue(said.startswith("On Saturday"), said)
        self.assertIn("начало в субботу", self.read(0))
        self.assertTrue(any("CHARSET UTF-8" in command for command in server.commands))


class Unchanged(Goa):
    """Nothing in the mailbox changes, and it is never downloaded."""

    async def test_mailboxes_are_only_examined_and_mail_is_never_marked_read(self):
        server = await self.mailbox(gmail(inbox=[(HELLO, 900), (PASSPORT, 13), (SCAM, 1)]))
        await self.ask()
        verbs = {command.split(" ")[1].upper() for command in server.commands}
        self.assertLessEqual(verbs, {"CAPABILITY", "AUTHENTICATE", "LIST", "EXAMINE", "UID",
                                     "LOGOUT"})
        fetches = [command for command in server.commands if " UID FETCH " in command.upper()]
        self.assertTrue(fetches)
        for fetch in fetches:
            self.assertNotIn("BODY[", fetch.upper().replace("BODY.PEEK[", ""))
            self.assertNotIn("RFC822", fetch.upper())
            self.assertRegex(fetch, r"<0\.\d+>")

    async def test_a_big_mailbox_is_never_downloaded(self):
        crowd = [(message(f"Passport note {n}", f"P{n} <p{n}@people.example>",
                          "About your passport renewal application."), 1 + n / 100)
                 for n in range(150)]
        server = await self.mailbox(gmail(inbox=crowd))
        await self.ask()
        for fetch in (c for c in server.commands if " UID FETCH " in c.upper() and "TEXT" in c):
            self.assertLessEqual(len(fetch.split(" ")[3].split(",")), goamail.FETCHED)
        fetched = sum(len(c.split(" ")[3].split(",")) for c in server.commands
                      if " UID FETCH " in c.upper() and "TEXT" in c)
        self.assertLessEqual(fetched, 2 * goamail.FETCHED)


class Secrets(Goa):
    async def test_a_code_is_hidden_and_cant_be_fished_for(self):
        code = message("One time code", OFFICE,
                       "Your code to sign in to the zebra portal is 482913. It expires soon.")
        await self.mailbox(gmail(inbox=[(HELLO, 900), (code, 1)]))
        await self.ask("What did the zebra portal mail say?")
        self.assertNotIn("482913", self.read(0))
        self.assertIn(f"portal is {mail.HIDDEN}", self.read(0))
        before = len(self.model)
        self.assertEqual(await self.ask("482913"), ("", {"message": mail.NOTHING_FOUND}))
        self.assertEqual(len(self.model), before)

    async def test_a_code_one_digit_to_a_block_is_hidden_once_its_lines_are_joined(self):
        """An HTML mail can put each digit of a code in a block of its own, which reads as one
        digit to a line until the lines are joined for the model."""
        layouts = ('<div style="display:inline-block">{}</div>', "<tr><td>{}</td></tr>")
        mails = [message(f"Verify your Acme account {n}", "Acme <account@acme.example>", None,
                         html="<p>Hello, here is the code to verify your Acme account:</p><table>"
                              + "".join(layout.format(digit) for digit in "482917")
                              + "</table><p>Thanks, Acme</p>")
                 for n, layout in enumerate(layouts)]
        await self.mailbox(gmail(inbox=[(HELLO, 900), *((raw, 1) for raw in mails)]))
        self.says = ["NOTHING", "NOTHING"]
        await self.ask("Did Acme email me about my account?")
        self.assertEqual(len(self.model), 2)
        for call in range(2):
            self.assertIn("verify your Acme account", self.read(call))
            self.assertIn(mail.HIDDEN, self.read(call))
            self.assertNotRegex(self.read(call), r"4\D{0,9}8\D{0,9}2\D{0,9}9\D{0,9}1\D{0,9}7")

    async def test_a_code_one_character_to_a_block_is_hidden_wherever_its_word_is(self):
        """With no word for a code near it, or with letters in it, it is hidden as it would be
        as plain lines: a code with a line to itself."""
        far = " We are always here to help you with anything you need." * 4
        layouts = (
            ("Enter this to finish signing in to your Acme account:", "482917", "<div>{}</div>"),
            ("Here is the code to verify your Acme account:", "KQ7X9B", "<div>{}</div>"),
            ("Your verification code for your Acme account." + far, "482917", "<div>{}</div>"),
            ("Here is what you need for your Acme account:", "KQ7X9B", "<td>{}</td>"))
        mails = [message(f"About your Acme account {n}", "Acme <account@acme.example>", None,
                         html=f"<p>Hello. {said}</p><table><tr>"
                              + "".join(layout.format(c) for c in code)
                              + "</tr></table><p>Thanks, Acme</p>")
                 for n, (said, code, layout) in enumerate(layouts)]
        await self.mailbox(gmail(inbox=[(HELLO, 900), *((raw, 1) for raw in mails)]))
        self.says = ["NOTHING"] * len(mails)
        with mock.patch.object(mail, "TRIED", len(mails)):
            await self.ask("Did Acme email me about my account?")
        self.assertEqual(len(self.model), len(mails))
        for call in range(len(mails)):
            self.assertIn("Acme account", self.read(call))
            self.assertIn(mail.HIDDEN, self.read(call))
            for code in ("482917", "KQ7X9B"):
                self.assertNotRegex(self.read(call), r"\W{0,9}".join(code))

    async def test_a_code_alone_on_a_line_ended_by_crlf_is_hidden(self):
        """A real server sends a mail's lines ended by CRLF, and a plain-text code alone on its
        line, or spelt out one character to a line, is hidden as the same lines with LF are."""
        said = "Hello. Here is what you need for your Acme account:"
        codes = ("\n482917\n", "\nKQ7X9B\n", "\n".join("482917"), "\n".join("KQ7X9B"))
        mails = [message(f"About your Acme account {n}", "Acme <account@acme.example>",
                         f"{said}\n{code}\nThanks, Acme")
                 for n, code in enumerate(codes)]
        for raw in mails:
            self.assertIn(b"account:\r\n", raw)
        await self.mailbox(gmail(inbox=[(HELLO, 900), *((raw, 1) for raw in mails)]))
        self.says = ["NOTHING"] * len(mails)
        with mock.patch.object(mail, "TRIED", len(mails)):
            await self.ask("Did Acme email me about my account?")
        self.assertEqual(len(self.model), len(mails))
        for call in range(len(mails)):
            self.assertIn("Acme account", self.read(call))
            self.assertIn(mail.HIDDEN, self.read(call))
            for code in ("482917", "KQ7X9B"):
                self.assertNotRegex(self.read(call), r"\W{0,9}".join(code))

    async def test_asking_for_one_is_refused_before_any_account_is_asked(self):
        server = await self.mailbox(gmail(inbox=[(PASSPORT, 13)]))
        said, told = await self.ask("What is my sign in code for the passport application?")
        self.assertEqual((said, told), ("", {"message": mail.NO_CODES}))
        self.assertEqual((self.calls.made, server.commands), ([], []))


class Strangers(Goa):
    async def test_a_first_time_sender_is_read_after_a_known_one(self):
        await self.mailbox(gmail(inbox=[(HELLO, 300), (PASSPORT, 13), (SCAM, 1)]))
        self.says = ["On the 29th."]
        said, _ = await self.ask()
        self.assertIn("We will write to you again", self.read(0))
        self.assertIn("On the 29th. That's from an email from notify.citizen.example", said)
        self.assertNotIn(mail.NEW_SENDER, said)

    async def test_and_what_one_says_comes_with_a_warning(self):
        await self.mailbox(gmail(inbox=[(SCAM, 1)]))
        self.says = ["It was rejected."]
        said, _ = await self.ask()
        self.assertTrue(said.startswith("It was rejected. That's from an email from "
                                        "passport-support.example on "), said)
        self.assertTrue(said.endswith(mail.NEW_SENDER), said)

    async def test_the_date_a_sender_writes_doesnt_make_them_known(self):
        """The server's own date says when a mail came. The Date line is the sender's."""
        old_dated = message("Hello", "Passport Office <help@passport-support.example>", "Hi.",
                            dated=2000)
        scam = message("Urgent: your application to renew your passport",
                       "Passport Office <help@passport-support.example>",
                       "Your application was rejected.", dated=1500)
        await self.mailbox(gmail(inbox=[(old_dated, 2), (scam, 1)]))
        self.says = ["It was rejected."]
        said, _ = await self.ask()
        self.assertTrue(said.endswith(mail.NEW_SENDER), said)

    async def test_a_known_address_typed_into_the_name_is_not_that_sender(self):
        forged = message("Urgent: your application to renew your passport",
                         '"no-reply@notify.citizen.example" <help@evil.example>',
                         "Your application was rejected.")
        await self.mailbox(gmail(inbox=[(HELLO, 900), (forged, 1)]))
        self.says = ["It was rejected."]
        said, _ = await self.ask()
        self.assertIn("from evil.example", said)
        self.assertTrue(said.endswith(mail.NEW_SENDER), said)

    async def test_an_older_mail_with_the_address_only_in_its_name_doesnt_vouch(self):
        """The server's FROM search matches names too, so its finds are read again here."""
        decoy = message("Hello", '"help@passport-support.example" <x@elsewhere.example>', "Hi.")
        await self.mailbox(gmail(inbox=[(decoy, 900), (SCAM, 1)]))
        self.says = ["It was rejected."]
        said, _ = await self.ask()
        self.assertTrue(said.endswith(mail.NEW_SENDER), said)

    async def test_two_sender_lines_are_nobodys(self):
        double = (f"From: {OFFICE}\r\nFrom: help@evil.example\r\nSubject: Urgent: your "
                  "application to renew your passport\r\n\r\nRejected.\r\n").encode()
        await self.mailbox(gmail(inbox=[(HELLO, 900), (double, 1)]))
        self.says = ["It was rejected."]
        said, _ = await self.ask()
        self.assertIn("an unknown sender", said)
        self.assertTrue(said.endswith(mail.NEW_SENDER), said)

    async def test_mail_from_your_own_address_outside_sent_is_anyones(self):
        forged = message("Your application to renew your passport", f"Me <{USER}>",
                         "Your application was rejected. Pay the fee.")
        await self.mailbox(gmail(inbox=[(forged, 900), (forged, 1)]))
        self.says = ["It was rejected."]
        said, _ = await self.ask()
        self.assertNotIn("you sent", said)
        self.assertTrue(said.endswith(mail.NEW_SENDER), said)

    async def test_what_you_sent_is_said_to_be_yours(self):
        mine = message("Re: renew passport application", f"Me <{USER}>",
                       "I sent the application to renew my passport today.")
        await self.mailbox(gmail(sent=[(mine, 10)]))
        self.says = ["Today."]
        said, _ = await self.ask()
        self.assertIn("That's from an email you sent on", said)
        self.assertNotIn(mail.NEW_SENDER, said)


class Accounts(Goa):
    """Only a short-lived token is asked for, and only the account's own server gets it."""

    async def test_only_the_token_is_asked_for_and_only_the_server_gets_it(self):
        server = await self.mailbox(gmail(inbox=[(HELLO, 900), (PASSPORT, 13)]),
                                    account={"password": True})
        records = []
        handler = logging.Handler(logging.DEBUG)
        handler.emit = lambda record: records.append(record.getMessage())
        logging.getLogger().addHandler(handler)
        self.addCleanup(logging.getLogger().removeHandler, handler)
        kept, real = [], imaplib.IMAP4._log     # imaplib's own memory of what it sent

        def spy(imap, line):
            kept.append(line)
            return real(imap, line)

        with mock.patch.object(logging.getLogger(), "level", logging.DEBUG), \
                mock.patch.object(imaplib.IMAP4, "_log", spy):
            self.says = ["Then."]
            said, told = await self.ask()
        self.assertEqual(self.calls.made, ["GetAccessToken"])
        self.assertEqual(server.signed_in_with, [f"user={USER}\x01auth=Bearer {TOKEN}\x01\x01"])
        self.assertNotIn(TOKEN, json.dumps([said, told, self.model, records]))
        self.assertIn("AUTHENTICATE XOAUTH2", " ".join(kept))
        for secret in (TOKEN, base64.b64encode(f"user={USER}".encode()).decode()[:12]):
            self.assertNotIn(secret, " ".join(kept))

    async def test_an_account_with_a_password_is_left_alone(self):
        server = Server(CERT, KEY, gmail(inbox=[(PASSPORT, 13)]))
        self.addCleanup(server.close)
        await self.accounts({"host": server.host, "oauth2": False, "password": True})
        with self.assertRaisesRegex(RuntimeError, goamail.PASSWORD_ONLY):
            await self.ask()
        self.assertEqual((self.calls.made, server.commands), ([], []))

    async def test_every_account_that_can_be_used_is_searched(self):
        empty = Server(CERT, KEY, gmail(inbox=[(HELLO, 900)]))
        full = Server(CERT, KEY, gmail(inbox=[(HELLO, 900), (PASSPORT, 13)]))
        for server in (empty, full):
            self.addCleanup(server.close)
        await self.accounts({"host": empty.host}, {"host": "imap.unused.example", "oauth2": False,
                                                   "password": True}, {"host": full.host})
        self.says = ["Then."]
        said, _ = await self.ask()
        self.assertTrue(said.startswith("Then. That's from an email from notify.citizen"), said)
        self.assertEqual(self.calls.made, ["GetAccessToken", "GetAccessToken"])
        self.assertTrue(empty.signed_in_with and full.signed_in_with)

    async def test_a_server_whose_certificate_isnt_trusted_never_gets_the_token(self):
        server = await self.mailbox(gmail(inbox=[(PASSPORT, 13)]))
        with mock.patch.object(goamail, "CA_FILE", None), \
                self.assertRaisesRegex(RuntimeError, goamail.BAD_CERTIFICATE):
            await self.ask()
        self.assertEqual(server.signed_in_with, [])

    async def test_starttls_works_and_is_never_skipped(self):
        server = await self.mailbox(gmail(inbox=[(HELLO, 900), (PASSPORT, 13)]), "starttls",
                                    account={"ssl": False, "tls": True})
        self.says = ["Then."]
        self.assertTrue((await self.ask())[0].startswith("Then."))
        self.assertEqual(server.in_clear, [])
        self.assertEqual(len(server.signed_in_with), 1)

    async def test_a_yes_to_starttls_with_a_code_in_brackets_is_still_a_yes(self):
        server = await self.mailbox(gmail(inbox=[(HELLO, 900), (PASSPORT, 13)]), "starttls",
                                    account={"ssl": False, "tls": True})
        server.yes = "OK [ALERT] begin TLS"
        self.says = ["Then."]
        self.assertTrue((await self.ask())[0].startswith("Then."))
        self.assertEqual(server.in_clear, [])
        self.assertEqual(len(server.signed_in_with), 1)

    async def test_a_server_that_doesnt_offer_tls_never_gets_the_token(self):
        server = await self.mailbox(gmail(inbox=[(PASSPORT, 13)]), "plain",
                                    account={"ssl": False, "tls": True})
        with self.assertRaisesRegex(RuntimeError, goamail.UNREACHABLE):
            await self.ask()
        self.assertEqual(server.signed_in_with, [])

    async def test_answers_slipped_in_before_tls_are_refused(self):
        server = await self.mailbox(gmail(inbox=[(PASSPORT, 13)]), "starttls",
                                    account={"ssl": False, "tls": True},)
        server.inject = b"* CAPABILITY IMAP4rev1 AUTH=XOAUTH2\r\n"
        with self.assertRaisesRegex(RuntimeError, goamail.UNREACHABLE):
            await self.ask()
        self.assertEqual(server.signed_in_with, [])

    async def test_answers_slipped_in_before_starttlss_yes_are_refused(self):
        """Read as if they came over TLS, they could say the inbox is where sent mail is, and a
        stranger's mail would pass for the user's own."""
        server = await self.mailbox(
            [Box("INBOX", "\\HasNoChildren", [(HELLO, 900), (SCAM, 1)]), Box("Sent", "\\Sent")],
            "starttls", account={"ssl": False, "tls": True})
        server.inject_before = b'* LIST (\\Sent) "/" "inbox"\r\n'
        with self.assertRaisesRegex(RuntimeError, goamail.UNREACHABLE):
            await self.ask()
        self.assertEqual(server.signed_in_with, [])

    async def test_the_inbox_is_never_taken_for_sent_mail(self):
        """Whatever the server calls it, and in whatever case: mail in the inbox is anyone's."""
        inbox = [(HELLO, 900), (SCAM, 1)]
        await self.mailbox([Box("INBOX", "\\HasNoChildren", inbox),
                            Box("inbox", "\\HasNoChildren \\Sent", inbox),
                            Box("Inbox", "\\HasNoChildren \\Sent", inbox),
                            Box("Sent", "\\HasNoChildren \\Sent")])
        self.says = ["It was rejected."]
        said, _ = await self.ask()
        self.assertTrue(said.endswith(mail.NEW_SENDER), said)
        self.assertNotIn("you sent", said)

    async def test_slow_sign_ins_leave_time_to_read_the_answer(self):
        """Online Accounts fetches a token past its hour again from the provider, which can be
        slow, and three accounts' tokens are asked for at once, out of the search's own time."""
        server = Server(CERT, KEY, gmail(inbox=[(HELLO, 900), (PASSPORT, 13)]))
        self.addCleanup(server.close)
        await self.accounts(*({"host": server.host, "slow": 2} for _ in range(3)))
        self.says = ["Then."]
        started = time.monotonic()
        with mock.patch.object(mail, "LIMIT_S", 6), mock.patch.object(mail, "ANSWER_S", 1):
            said, _ = await self.ask()
        self.assertTrue(said.startswith("Then. That's from an email from notify.citizen"), said)
        self.assertEqual(self.calls.made, ["GetAccessToken"] * 3)
        self.assertLess(time.monotonic() - started, 6)

    async def test_tokens_that_dont_come_in_time_are_never_called_no_mail(self):
        """The accounts are there, Online Accounts was only slow to refresh their tokens."""
        await self.slow_tokens(*({"slow": 3} for _ in range(3)))

    async def test_and_that_is_said_before_an_account_that_was_left_alone(self):
        await self.slow_tokens({"host": "imap.unused.example", "oauth2": False, "password": True},
                               {"slow": 3})

    async def slow_tokens(self, *specs):
        server = Server(CERT, KEY, gmail(inbox=[(PASSPORT, 13)]))
        self.addCleanup(server.close)
        await self.accounts(*({"host": server.host, **spec} for spec in specs))
        started = time.monotonic()
        with mock.patch.object(goamail, "SIGN_IN_S", 0.5), \
                self.assertRaisesRegex(RuntimeError, goamail.NO_ANSWER):
            await self.ask()
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(server.commands, [])

    async def test_an_account_set_up_to_skip_checks_is_left_alone(self):
        for unsafe in ({"accept_errors": True}, {"ssl": False, "tls": False}):
            server = Server(CERT, KEY, gmail(inbox=[(PASSPORT, 13)]))
            self.addCleanup(server.close)
            await self.accounts({"host": server.host, **unsafe})
            with self.assertRaisesRegex(RuntimeError, goamail.NOT_SAFE):
                await self.ask()
            self.assertEqual((self.calls.made, server.commands), ([], []), unsafe)

    async def test_an_account_that_needs_signing_in_again_says_so(self):
        await self.mailbox(gmail(inbox=[(PASSPORT, 13)]), account={"attention": True})
        with self.assertRaisesRegex(RuntimeError, goamail.SIGN_IN_AGAIN):
            await self.ask()

    async def test_an_account_with_mail_turned_off_is_not_searched(self):
        server = await self.mailbox(gmail(inbox=[(PASSPORT, 13)]), account={"mail_disabled": True})
        with self.assertRaisesRegex(RuntimeError, mail.NO_INDEX):
            await self.ask()
        self.assertEqual(server.commands, [])

    async def test_switched_off_no_account_is_asked(self):
        server = await self.mailbox(gmail(inbox=[(PASSPORT, 13)]))
        with self.assertRaisesRegex(ValueError, mail.OFF):
            await self.ask(config={**CONFIG, "mail_search": False})
        self.assertEqual((self.calls.made, server.commands), ([], []))

    def test_the_server_address_from_online_accounts(self):
        def settings(host, **more):
            values = {"ImapHost": host, "ImapUserName": USER, "ImapUseSsl": True,
                      "ImapUseTls": False, "ImapAcceptSslErrors": False, **more}
            return {goamail.ACCOUNT: {"MailDisabled": imap_fake_variant(False)},
                    goamail.MAIL: {k: imap_fake_variant(v) for k, v in values.items()},
                    goamail.OAUTH2: {}}
        account = goamail._account("/a", settings("imap.mailbox.example"))
        self.assertEqual((account.host, account.port, account.starttls),
                         ("imap.mailbox.example", 993, False))
        self.assertEqual(goamail._account("/a", settings("[::1]:1993"))[2:4], ("::1", 1993))
        starttls = goamail._account("/a", settings("mail.example", ImapUseSsl=False,
                                                   ImapUseTls=True))
        self.assertEqual((starttls.port, starttls.starttls), (143, True))
        for bad in ("", "imap.example:99999", "imap example", "imap.example\r\nX"):
            self.assertIsNone(goamail._account("/a", settings(bad)), bad)


def imap_fake_variant(value):
    from dbus_fast import Variant
    return Variant("b" if isinstance(value, bool) else "s", value)


class HostileServer(Goa):
    async def test_a_server_that_sends_more_than_asked_for_is_cut_off(self):
        server = await self.mailbox(gmail(inbox=[(PASSPORT, 13)]))
        server.huge = True
        started = time.monotonic()
        self.assertEqual(await self.ask(), ("", {"message": mail.NOTHING_FOUND}))
        self.assertLess(time.monotonic() - started, 3)

    async def test_a_server_that_sends_too_much_in_all_is_cut_off(self):
        """Each answer is small enough, but none is cut to what was asked for."""
        crowd = [(message(f"Passport note {n}", OFFICE, "Your passport renewal application."), 2)
                 for n in range(goamail.FETCHED)]
        server = await self.mailbox(gmail(inbox=crowd))
        server.fat = goamail.MAX_LITERAL
        self.assertEqual(await self.ask(), ("", {"message": mail.NOTHING_FOUND}))
        self.assertEqual(self.model, [])

    async def test_a_server_that_stalls_is_given_up_on_in_time(self):
        server = await self.mailbox(gmail(inbox=[(PASSPORT, 13)]))
        server.stall = 30
        started = time.monotonic()
        with mock.patch.object(goamail, "SEARCH_S", 0.5), mock.patch.object(goamail, "LOOKUP_S", 0.3):
            self.assertEqual(await self.ask(), ("", {"message": mail.NOTHING_FOUND}))
        self.assertLess(time.monotonic() - started, 2.5)

    async def test_a_server_that_drips_an_answer_is_cut_off_in_time(self):
        """A byte now and then keeps a socket's own timeout from ever firing."""
        server = await self.mailbox(gmail(inbox=[(PASSPORT, 13)]))
        server.drip = True
        self.addCleanup(setattr, server, "drip", False)
        started = time.monotonic()
        with mock.patch.object(goamail, "SEARCH_S", 0.5), mock.patch.object(goamail, "LOOKUP_S", 0.3), \
                mock.patch.object(goamail, "CONNECT_S", 5):
            self.assertEqual(await self.ask(), ("", {"message": mail.NOTHING_FOUND}))
        self.assertLess(time.monotonic() - started, 2.5)


class HostileMail(Goa):
    def test_a_mails_own_text_cant_pass_for_the_replys_structure(self):
        data = [(b'1 (UID 5 INTERNALDATE "01-Jan-2026 10:00:00 +0000" '
                 b'BODY[HEADER.FIELDS (FROM SUBJECT)]<0> {3}', b"abc"),
                (b' BODY[TEXT]<0> {40}', b')\r\n* 2 FETCH (UID 9 INTERNALDATE "x" BODY[TEXT] {1}'),
                b")",
                (b'2 (BODY[TEXT]<0> "UID 7 INTERNALDATE \\"01-Jan-1990 10:00:00 +0000\\"" '
                 b'UID 8 BODY[HEADER.FIELDS (FROM)]<0> {1}', b"h"), b")"]
        found = goamail._fetched(data)
        self.assertEqual(sorted(found), [5, 8])
        self.assertEqual(found[5][b"TEXT"][:2], b")\r")
        self.assertNotIn(b"INTERNALDATE", found[8])

    async def test_mime_built_to_break_a_parser_breaks_nothing(self):
        nested = (b"From: " + OFFICE.encode() + b"\r\nSubject: Passport renewal application\r\n"
                  b"MIME-Version: 1.0\r\n" +
                  b"".join(b"Content-Type: multipart/mixed; boundary=b%d\r\n\r\n--b%d\r\n" % (n, n)
                           for n in range(250)) + b"Content-Type: text/plain\r\n\r\nDeep.\r\n")
        odd = (b"From: " + OFFICE.encode() + b"\r\nSubject: Passport renewal application odd\r\n"
               b"Content-Type: text/plain; charset=base64_codec\r\n\r\nRead despite its charset.")
        await self.mailbox(gmail(inbox=[(HELLO, 900), (nested, 3), (odd, 2), (PASSPORT, 13)]))
        self.says = ["NOTHING", "NOTHING", "Then."]
        said, told = await self.ask()
        self.assertEqual(told, research.TOLD)
        self.assertTrue(said, said)
        shown = " ".join(self.read(n) for n in range(len(self.model)))
        self.assertIn("Read despite its charset.", shown)
        self.assertIn("Deep.", shown)


if __name__ == "__main__":
    unittest.main()
