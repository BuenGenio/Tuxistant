"""Translation: what the model calls a language, what the server gets, what the model never sees.

The live checks are skipped unless asked for. LibreTranslate needs no key:

    TUXISTANT_LIVE=1 TUXISTANT_TRANSLATE_SERVER=host:5000 python3 -m unittest tests.test_translate
"""
import json
import os
import socket
import sys
import threading
import time
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tests.test_daemon_fakes import CHUNK, Case, tone  # noqa: E402
from tuxistant import translate  # noqa: E402
from tuxistant.config import DEFAULTS  # noqa: E402
from tuxistant.gate import Ask, Session, Speak  # noqa: E402
from tuxistant.realtime import RealtimeError  # noqa: E402
from tuxistant.skills import Skills, leaves_home, reads_private  # noqa: E402
from tuxistant.translate import FAILED, LANGUAGES, VOICES, find_language  # noqa: E402

HELLO = "Добрий день"
UK = VOICES["uk"]
ES = VOICES["es"]
VOICE = tone(1.0)


class FakeTranslator:
    """LibreTranslate on a loopback port, in its own thread."""

    def __init__(self, status=200, body=None, delay=0.0, location=None):
        self.requests = []
        outer = self
        reply = json.dumps({"translatedText": HELLO}).encode() if body is None else body

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):       # what urllib turns a redirected POST into
                outer.requests.append((self.path, None))
                self.answer()

            def do_POST(self):
                outer.requests.append(
                    (self.path, json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
                self.answer()

            def answer(self):
                time.sleep(delay)
                self.send_response(status)
                if location:
                    self.send_header("Location", location)
                self.send_header("Content-Length", str(len(reply)))
                self.end_headers()
                self.wfile.write(reply)

        self.http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.http.server_address[1]
        threading.Thread(target=self.http.serve_forever, args=(0.01,), daemon=True).start()

    def close(self):
        self.http.shutdown()
        self.http.server_close()


class Translating(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        translate._known.clear()
        translate._retry_at = 0.0
        self.addCleanup(translate._known.clear)
        patcher = mock.patch.object(translate, "list_models", return_value=[UK, ES, "tux-voice"])
        self.list_models = patcher.start()
        self.addCleanup(patcher.stop)
        self.skills = Skills()

    def translator(self, **options) -> FakeTranslator:
        server = FakeTranslator(**options)
        self.addCleanup(server.close)
        self.skills.config = {**DEFAULTS, "server": "127.0.0.1:8080",
                              "translate_server": f"127.0.0.1:{server.port}"}
        return server

    async def act(self, tool: str, arguments: dict, session: Session | None = None):
        session = session or Session(self.skills)
        return (await self.skills.execute(tool, json.dumps(arguments), session))[1], session

    async def text(self, **arguments):
        return await self.act("translate_text", arguments)

    def selecting(self, text: str) -> Session:
        async def selection():
            return text
        return Session(self.skills, selection=selection)


class Names(unittest.TestCase):
    def test_what_the_model_says(self):
        for said, code in (
                ("Ukrainian", "uk"), ("ukrainian", "uk"), ("Ukranian", "uk"), ("Spanish", "es"),
                ("Mexican Spanish", "es"), ("Latin American Spanish", "es"), ("Mexican", "es"),
                ("Spanish (Mexico)", "es"), ("Chinese", "zh-Hans"), ("Mandarin", "zh-Hans"),
                ("Simplified Chinese", "zh-Hans"), ("Mandarin Chinese", "zh-Hans"),
                ("Traditional Chinese", "zh-Hant"), ("Chinese (traditional)", "zh-Hant"),
                ("Chinese (Taiwan)", "zh-Hant"), ("Chinese (Mandarin)", "zh-Hans"),
                ("Brazilian Portuguese", "pt-BR"), ("Portuguese (Brazil)", "pt-BR"),
                ("Brazilian", "pt-BR"), ("European Portuguese", "pt"), ("Portuguese", "pt"),
                ("Farsi", "fa"), ("Persian", "fa"), ("Swiss German", "de"), ("Filipino", "tl"),
                ("Canadian French", "fr"), ("Español", "es"), ("Deutsch", "de"),
                ("the Spanish language", "es"), ("in Spanish", "es"), ("  into  German ", "de"),
                ("English to Spanish", "es"), ("the Brazilian Portuguese language", "pt-BR")):
            with self.subTest(said):
                self.assertEqual(find_language(said), code)

    def test_codes_however_they_are_written(self):
        for said, code in (
                ("uk", "uk"), ("UK", "uk"), ("es", "es"), ("pt-BR", "pt-BR"), ("pt_br", "pt-BR"),
                ("PT-BR", "pt-BR"), ("zh-Hans", "zh-Hans"), ("zh_Hant", "zh-Hant"),
                ("zh", "zh-Hans"), ("ZH", "zh-Hans"), ("zh-CN", "zh-Hans"), ("zh-TW", "zh-Hant"),
                ("zh-SG", "zh-Hans"), ("es-MX", "es"), ("es_ES", "es"), ("uk-UA", "uk"),
                ("en-GB", "en"), ("fr-CA", "fr"), ("pt-PT", "pt"), ("ua", "uk"), ("jp", "ja"),
                ("no", "nb")):
            with self.subTest(said):
                self.assertEqual(find_language(said), code)

    def test_every_language_is_found_by_code_and_by_name(self):
        for code, name in LANGUAGES.items():
            with self.subTest(code):
                self.assertEqual(find_language(code), code)
                self.assertEqual(find_language(name), code)

    def test_what_is_not_there_is_not_guessed(self):
        for said in ("Klingon", "Latin", "Scottish Gaelic", "Haitian Creole", "Serbian", "Croatian",
                     "Cantonese", "xx", "", "   ", "12", "no way", "to", "sign language",
                     "Traditional", "x-y"):
            with self.subTest(said):
                self.assertIsNone(find_language(said))

    def test_every_voice_belongs_to_a_language(self):
        languages = {code.partition("-")[0] for code in LANGUAGES}
        self.assertLessEqual(set(VOICES), languages)


class Server(unittest.TestCase):
    def base(self, **settings) -> str:
        return translate._base({**DEFAULTS, **settings})

    def test_the_setting_wins(self):
        self.assertEqual(self.base(translate_server="lt.lan:5001"), "http://lt.lan:5001")
        self.assertEqual(self.base(translate_server="https://lt.lan"), "https://lt.lan")

    def test_otherwise_it_is_on_the_host_of_the_server(self):
        for server, base in (("stormshadow:8080", "http://stormshadow:5000"),
                             ("stormshadow", "http://stormshadow:5000"),
                             ("10.0.0.2:8080", "http://10.0.0.2:5000"),
                             ("[::1]:8080", "http://[::1]:5000"), ("[::1]", "http://[::1]:5000"),
                             ("https://box.lan:8443", "https://box.lan:5000")):
            with self.subTest(server):
                self.assertEqual(self.base(server=server), base)


class Text(Translating):
    async def test_the_server_gets_a_plain_request(self):
        server = self.translator()
        await self.text(text="  good day ", to="Ukrainian")
        self.assertEqual(server.requests, [("/translate", {
            "q": "good day", "source": "auto", "target": "uk", "format": "text"})])

    async def test_the_model_hears_only_that_it_is_done(self):
        self.translator()
        result, session = await self.text(text="good day", to="Ukrainian")
        self.assertEqual(result, {"done": True, "language": "Ukrainian", "spoken": True})
        self.assertEqual(session.after, [Speak(HELLO, UK, UK)])

    async def test_the_language_is_named_by_us_and_not_by_the_model(self):
        self.translator()
        result, _ = await self.text(text="hi", to="Mexican Spanish")
        self.assertEqual(result["language"], "Spanish")

    async def test_a_source_it_names_is_used(self):
        server = self.translator()
        await self.act("translate_text", {"text": "hi", "to": "Spanish", "from": "German"})
        self.assertEqual(server.requests[0][1]["source"], "de")

    async def test_a_doubtful_source_is_left_to_the_server(self):
        for source in ("auto", "unknown", "Klingon", "", None, 7, "Spanish", "es"):
            with self.subTest(source):
                server = self.translator()
                await self.act("translate_text", {"text": "hi", "to": "es", "from": source})
                self.assertEqual(server.requests[0][1]["source"], "auto")

    async def test_no_voice_installed_means_shown_and_not_spoken(self):
        self.translator()
        self.list_models.return_value = ["tux-voice"]
        result, session = await self.text(text="hi", to="uk")
        self.assertFalse(result["spoken"])
        self.assertEqual(session.after, [Speak(HELLO)])

    async def test_a_language_with_no_voice_does_not_ask_the_server_for_one(self):
        self.translator()
        result, session = await self.text(text="hi", to="Japanese")
        self.assertEqual(result, {"done": True, "language": "Japanese", "spoken": False})
        self.assertEqual(session.after, [Speak(HELLO)])
        self.list_models.assert_not_called()

    async def test_languages_that_share_a_voice(self):
        self.translator()
        self.list_models.return_value = list(VOICES.values())
        for to, language in (("pt", "pt"), ("pt-BR", "pt"), ("zh-Hans", "zh"), ("zh-Hant", "zh")):
            with self.subTest(to):
                _, session = await self.text(text="hi", to=to)
                self.assertEqual(session.after[0].model, VOICES[language])

    async def test_english_goes_to_the_normal_voice(self):
        self.translator()
        result, session = await self.text(text="hola", to="English")
        self.assertEqual(result, {"done": True, "language": "English", "spoken": True})
        self.assertEqual(session.after, [Speak(HELLO, DEFAULTS["voice"], None)])
        self.list_models.assert_not_called()

    async def test_arguments_of_the_wrong_type_are_not_a_crash(self):
        self.translator()
        for arguments in ({"text": 12, "to": "es"}, {"text": "hi", "to": 12},
                          {"text": ["hi"], "to": ["es"]}, {"text": None, "to": None}):
            with self.subTest(arguments):
                result, _ = await self.act("translate_text", arguments)
                self.assertTrue(result.get("done") or "error" in result)


class Refusals(Translating):
    def assert_refused(self, result, session, wanted, server=None):
        self.assertEqual(result, {"error": wanted})
        self.assertEqual(session.after, [])
        if server:
            self.assertEqual(server.requests, [])

    async def test_a_language_the_server_lacks(self):
        server = self.translator()
        result, session = await self.text(text="hi", to="Klingon")
        self.assert_refused(result, session, "I can't translate into Klingon.", server)

    async def test_a_made_up_name_comes_back_short(self):
        self.translator()
        result, _ = await self.text(text="hi", to="x" * 500 + "\n\tyy")
        self.assertLess(len(result["error"]), 80)

    async def test_no_language(self):
        server = self.translator()
        result, session = await self.text(text="hi")
        self.assert_refused(result, session, "Which language should I translate into?", server)

    async def test_nothing_to_translate(self):
        server = self.translator()
        for text in ("", "   ", None):
            with self.subTest(text):
                result, session = await self.act("translate_text", {"text": text, "to": "es"})
                self.assert_refused(result, session, "I didn't catch what to translate.", server)

    async def test_the_length_is_capped(self):
        server = self.translator()
        limit = translate.MAX_TEXT_CHARS
        result, session = await self.text(text="x" * (limit + 1), to="es")
        self.assertIn("too long", result["error"])
        self.assert_refused(result, session, result["error"], server)
        self.assertTrue((await self.text(text="x" * limit, to="es"))[0]["done"])

    async def test_a_language_this_server_turns_down(self):
        self.translator(status=400, body=b'{"error":"es is not supported"}')
        result, session = await self.text(text="hi", to="es")
        self.assert_refused(result, session, "The translation server can't translate that into "
                                             "Spanish.")

    async def test_a_server_that_fails(self):
        for status in (403, 404, 429, 500, 503):
            with self.subTest(status):
                self.translator(status=status, body=b"secret detail")
                result, session = await self.text(text="hi", to="es")
                self.assert_refused(result, session, FAILED)

    async def test_a_server_that_talks_nonsense(self):
        for body in (b"", b"<html>", b"[1]", b"7", b"{}", b'{"translatedText": ""}',
                     b'{"translatedText": "  "}', b'{"translatedText": 5}',
                     b'{"translatedText": null}'):
            with self.subTest(body):
                self.translator(body=body)
                result, session = await self.text(text="hi", to="es")
                self.assert_refused(result, session, FAILED)

    async def test_an_answer_too_big_to_read_whole(self):
        self.translator()
        with mock.patch.object(translate, "MAX_BYTES", 5):
            result, session = await self.text(text="hi", to="es")
        self.assert_refused(result, session, FAILED)

    async def test_no_server(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        self.skills.config = {**DEFAULTS, "server": "127.0.0.1:8080",
                              "translate_server": f"127.0.0.1:{port}"}
        result, session = await self.text(text="hi", to="es")
        self.assert_refused(result, session, FAILED)

    async def test_a_server_address_that_makes_no_sense(self):
        self.skills.config = {**DEFAULTS, "translate_server": "bad host:port"}
        result, session = await self.text(text="hi", to="es")
        self.assert_refused(result, session, FAILED)

    async def test_a_server_that_hangs(self):
        self.translator(delay=1)
        with mock.patch.object(translate, "TIMEOUT_S", 0.05):
            result, session = await self.text(text="hi", to="es")
        self.assert_refused(result, session, FAILED)


class Reach(Translating):
    async def test_a_redirect_is_not_followed(self):
        elsewhere = self.translator()
        self.translator(status=302, location=f"http://127.0.0.1:{elsewhere.port}/translate")
        result, session = await self.text(text="private words", to="es")
        self.assertEqual(result, {"error": FAILED})
        self.assertEqual(elsewhere.requests, [])
        self.assertEqual(session.after, [])

    async def test_a_proxy_is_not_used(self):
        proxy = self.translator()
        server = self.translator()
        # urllib's own opener reads the environment once, so it has to be made again to see this.
        self.addCleanup(urllib.request.install_opener, None)
        urllib.request.install_opener(None)
        with mock.patch.dict(os.environ, {"http_proxy": f"http://127.0.0.1:{proxy.port}",
                                          "no_proxy": ""}):
            await self.text(text="private words", to="es")
        self.assertEqual(proxy.requests, [])
        self.assertEqual(len(server.requests), 1)


class Models(Translating):
    async def test_the_list_is_fetched_once(self):
        self.translator()
        for _ in range(3):
            await self.text(text="hi", to="uk")
        self.list_models.assert_called_once()

    async def test_each_server_has_its_own_list(self):
        self.translator()
        await self.text(text="hi", to="uk")
        self.skills.config = {**self.skills.config, "server": "elsewhere:8080"}
        self.list_models.return_value = []
        self.assertFalse((await self.text(text="hi", to="uk"))[0]["spoken"])

    async def test_a_failure_costs_only_the_voice_and_is_tried_again_soon(self):
        self.translator()
        for failure in (RealtimeError("Cannot reach"), KeyError("id"), AttributeError("get")):
            with self.subTest(failure):
                translate._retry_at = 0.0
                self.list_models.reset_mock()
                self.list_models.side_effect = failure
                for _ in range(2):
                    result, session = await self.text(text="hi", to="uk")
                    self.assertEqual(result, {"done": True, "language": "Ukrainian",
                                              "spoken": False})
                    self.assertEqual(session.after, [Speak(HELLO)])
                self.list_models.assert_called_once()

        self.assertLessEqual(translate._retry_at, time.monotonic() + translate.MODELS_RETRY_S)
        self.assertGreater(translate._retry_at, time.monotonic())
        self.list_models.side_effect = None
        translate._retry_at = 0.0       # the wait has passed
        self.assertTrue((await self.text(text="hi", to="uk"))[0]["spoken"])


class Selection(Translating):
    async def test_the_selection_is_translated(self):
        server = self.translator()
        result, session = await self.act("translate_selection", {"to": "Ukrainian"},
                                         self.selecting("  good day\n"))
        self.assertEqual(result, {"done": True, "language": "Ukrainian", "spoken": True})
        self.assertEqual(session.after, [Speak(HELLO, UK, UK)])
        self.assertEqual(server.requests[0][1], {"q": "good day", "source": "auto", "target": "uk",
                                                 "format": "text"})

    async def test_nothing_selected(self):
        server = self.translator()
        for text in ("", " \n "):
            with self.subTest(text):
                result, session = await self.act("translate_selection", {"to": "es"},
                                                 self.selecting(text))
                self.assertEqual(result, {
                    "error": "Nothing is selected. Select some text and ask again."})
                self.assertEqual(session.after, [])
                self.assertFalse(session.tainted)
        self.assertEqual(server.requests, [])

    async def test_no_shell_to_ask_means_nothing_selected(self):
        self.translator()
        result, _ = await self.act("translate_selection", {"to": "es"})
        self.assertIn("Nothing is selected", result["error"])

    async def test_a_bad_language_does_not_read_the_selection(self):
        self.translator()
        read = []

        async def selection():
            read.append(1)
            return "private words"
        for arguments in ({"to": "Klingon"}, {}):
            with self.subTest(arguments):
                result, session = await self.act("translate_selection", arguments,
                                                 Session(self.skills, selection=selection))
                self.assertIn("error", result)
                self.assertFalse(session.tainted)
        self.assertEqual(read, [])

    async def test_a_long_selection_is_refused_and_never_sent(self):
        server = self.translator()
        result, session = await self.act("translate_selection", {"to": "es"}, self.selecting(
            "x" * (translate.MAX_TEXT_CHARS + 1)))
        self.assertIn("too long", result["error"])
        self.assertEqual(server.requests, [])
        self.assertFalse(session.tainted)

    async def test_the_network_stays_off_the_event_loop(self):
        self.translator()
        threads = []

        def request(*_):
            threads.append(threading.current_thread())
            return HELLO
        with mock.patch.object(translate, "_request", request):
            await self.act("translate_selection", {"to": "es"}, self.selecting("hi"))
        self.assertEqual(len(threads), 1)
        self.assertIsNot(threads[0], threading.current_thread())

    async def test_a_selection_taints_the_conversation_and_spoken_words_do_not(self):
        self.translator()
        self.assertTrue((await self.act("translate_selection", {"to": "es"},
                                        self.selecting("hi")))[1].tainted)
        self.assertFalse((await self.text(text="hi", to="es"))[1].tainted)


class Manifests(Translating):
    def test_the_permissions(self):
        text = self.skills.actions["translate.text"]
        selection = self.skills.actions["translate.selection"]
        self.assertEqual(text["permissions"], {"network": ["home"]})
        self.assertEqual(selection["permissions"], {"network": ["home"], "clipboard": True})
        self.assertEqual((text["risk"], selection["risk"]), ("medium", "medium"))
        self.assertFalse(reads_private(text["permissions"]))
        self.assertTrue(reads_private(selection["permissions"]))

    def test_the_model_sees_few_parameters(self):
        tools = {tool["name"]: tool["parameters"] for tool in self.skills.tools()}
        self.assertEqual(set(tools["translate_text"]["properties"]), {"text", "to", "from"})
        self.assertEqual(tools["translate_text"]["required"], ["text", "to"])
        self.assertEqual(set(tools["translate_selection"]["properties"]), {"to"})
        self.assertEqual(tools["translate_selection"]["required"], ["to"])

    async def test_it_stays_home_so_no_card_is_needed_even_after_private_data(self):
        self.translator()
        cards = []

        async def confirm(ask):
            cards.append(ask)
            return False
        for name in ("translate.text", "translate.selection"):
            self.assertFalse(leaves_home(self.skills.actions[name]["permissions"]))
        session = Session(self.skills, confirm)
        session.tainted = True
        result, _ = await self.act("translate_text", {"text": "hi", "to": "es"}, session)
        self.assertTrue(result["done"])
        self.assertEqual(cards, [])


class WholeTurn(Case):
    """The real client and controller: a translation from the tool call to the speaker."""

    def setUp(self):
        super().setUp()
        translate._known.clear()
        translate._retry_at = 0.0
        self.addCleanup(translate._known.clear)
        self.translator = FakeTranslator()
        self.addCleanup(self.translator.close)
        self.said = []

    async def speech(self, _config, text, voice, model=None):
        self.said.append((text, voice, model))
        return tone(0.1)

    async def run_turn(self, tool: str, arguments: dict, models=(UK,)):
        async def script(c):
            await c.heard_audio(CHUNK)
            await c.committed("translate this")
            await c.tool_call(tool, arguments=json.dumps(arguments))
            await c.done(output=[{"type": "function_call"}])
            await c.expect("conversation.item.create")
            await c.expect("response.create")
            await c.reply("Done.")

        self.conversation = await self.server(script)
        rig = self.rig(self.conversation.port, mic_pcm=VOICE,
                       translate_server=f"127.0.0.1:{self.translator.port}")
        rig.skills.config = rig.config
        shown = rig.ctl.emit

        def emit(name, *args):      # what the extension does when the daemon wants the selection
            shown(name, *args)
            if name == "SelectionWanted":
                rig.ctl.selection_text(args[0], "good day")
        rig.ctl.emit = emit
        with (mock.patch.object(translate, "list_models", return_value=list(models)),
              mock.patch("tuxistant.controller.speech", self.speech)):
            await rig.ctl.toggle()
            await rig.finished()
        return rig

    def model_got(self) -> dict:
        for event in self.conversation.conn.log:
            self.assertNotIn(HELLO, json.dumps(event, ensure_ascii=False))
        created = [e for e in self.conversation.conn.log if e["type"] == "conversation.item.create"]
        return json.loads(created[0]["item"]["output"])

    async def test_said_after_the_reply_in_its_own_voice_and_never_shown_to_the_model(self):
        rig = await self.run_turn("translate_text", {"text": "good day", "to": "Ukrainian"})
        self.assertEqual(self.model_got(), {"done": True, "language": "Ukrainian", "spoken": True})
        self.assertEqual(rig.of("Reply"), ["Done.", HELLO])
        self.assertEqual(self.said, [(HELLO, UK, UK)])
        self.assertEqual(rig.of("Error"), [])
        self.assertEqual(rig.history.recent()[0]["reply"], f"Done. {HELLO}")

    async def test_the_selection_comes_from_the_shell_the_same_way(self):
        rig = await self.run_turn("translate_selection", {"to": "uk"})
        self.assertEqual(self.model_got(), {"done": True, "language": "Ukrainian", "spoken": True})
        self.assertEqual(self.translator.requests[0][1]["q"], "good day")
        self.assertEqual(self.said, [(HELLO, UK, UK)])
        self.assertNotIn("good day", json.dumps(rig.history.recent()[0]["actions"]))

    async def test_without_a_voice_it_is_shown_and_nothing_is_synthesised(self):
        rig = await self.run_turn("translate_text", {"text": "good day", "to": "uk"}, models=())
        self.assertEqual(self.model_got(), {"done": True, "language": "Ukrainian", "spoken": False})
        self.assertEqual(rig.of("Reply"), ["Done.", HELLO])
        self.assertEqual(self.said, [])
        self.assertEqual(rig.of("Error"), [])


@unittest.skipUnless(os.environ.get("TUXISTANT_LIVE"), "live checks only when asked for")
class Live(unittest.TestCase):
    def setUp(self):
        self.config = {**DEFAULTS, "translate_server": os.environ["TUXISTANT_TRANSLATE_SERVER"]}

    def test_the_table_matches_the_server(self):
        url = f"{translate._base(self.config)}/languages"
        with translate._opener.open(url, timeout=translate.TIMEOUT_S) as response:
            self.assertEqual({l["code"] for l in json.load(response)}, set(LANGUAGES))

    def test_a_real_translation(self):
        for target, wanted in (("uk", "ранку"), ("es", "Buenos"), ("en", "morning")):
            with self.subTest(target):
                text = translate._request("Good morning, how are you?" if target != "en"
                                          else "Buenos días", "auto", target, self.config)
                self.assertIn(wanted.lower(), text.lower())

    def test_the_cases_the_server_refuses(self):
        with self.assertRaisesRegex(RuntimeError, "can't translate that"):
            translate._request("hi", "xx", "es", self.config)


class SomeoneElsesServer(Translating):
    """"home" in the manifest only holds while the translator is the voice server's machine."""

    def setUp(self):
        super().setUp()
        self.translator()
        self.skills.config = {**self.skills.config, "server": "stormshadow:8080"}

    def test_private_text_does_not_leave_without_a_card(self):
        session = Session()
        session.tainted = True
        ask = translate.translate_text({"text": "from a file", "to": "Spanish"},
                                       self.skills.config, session)
        self.assertIsInstance(ask, Ask)
        self.assertIn("127.0.0.1", ask.title)
        self.assertEqual(ask.detail, "from a file")

    def test_spoken_words_need_no_card(self):
        result = translate.translate_text({"text": "hello", "to": "Spanish"}, self.skills.config,
                                          Session())
        self.assertTrue(result["done"])

    def test_the_same_machine_on_another_port_is_still_home(self):
        config = {**self.skills.config, "server": "127.0.0.1:8080"}
        session = Session()
        session.tainted = True
        self.assertTrue(translate.translate_text({"text": "hi", "to": "Spanish"}, config,
                                                 session)["done"])


if __name__ == "__main__":
    unittest.main()
