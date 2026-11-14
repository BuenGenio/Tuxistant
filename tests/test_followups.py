"""Follow-ups: a hold soon after the last one carries on its conversation. See "Follow-ups" in
docs/CONTRACT.md. A scripted server and fake audio, as in test_daemon_controller."""
import asyncio
import json
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tests.test_daemon_fakes import Case, CountingSkills, tone  # noqa: E402
from tuxistant import controller, planner, service, skills  # noqa: E402
from tuxistant.controller import Talk, _Fixed  # noqa: E402
from tuxistant.gate import Ask, Speak  # noqa: E402
from tuxistant.planner import Reply  # noqa: E402

VOICE = tone(1.0)
QUESTION = "I found two reports. Which one should I print?"


def private(_args, _config, session):
    """A handler that reads something private, as file.read does."""
    session.tainted = True
    return {"time": "noon"}


class Model:
    """Stands in for the planner's local model: says what it is given, in order."""
    leaves_home = False
    wait_s = 5
    made = []
    script = []

    def __init__(self, config, goal, _tools):
        self.config, self.sent_to = config, planner._sent_to(config)    # as the real ones do
        self.heard = [goal]
        Model.made.append(self)

    def hear(self, answer):
        self.heard.append(answer)

    async def step(self, _outcomes, _last):
        return Reply(Model.script.pop(0), [])


class FollowUps(Case):
    def setUp(self):
        super().setUp()
        self.said = []      # what each hold hears, in order
        self.sorted = []    # what was checked for being a fact
        self.speech = mock.AsyncMock(return_value=tone(0.1))

        async def transcribe(_config, _pcm):
            return self.said.pop(0)

        async def is_fact(heard, _config):
            self.sorted.append(heard)
            return False

        Model.made, Model.script = [], []
        for target, name, value in ((controller, "transcribe", transcribe),
                                    (controller.knowledge, "is_fact", is_fact),
                                    (controller, "speech", self.speech),
                                    (planner, "Local", Model)):
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    async def rigged(self, script, **options):
        server = await self.server(script)
        rig = self.rig(server.port, mic_pcm=VOICE, mic={"block": True}, **options)
        return server, rig

    async def hold(self, rig, words="what time is it"):
        self.said.append(words)
        await rig.ctl.hold_start()
        await rig.state("listening")
        await asyncio.sleep(0.15)   # the fake mic gets through its audio
        await rig.ctl.hold_end()
        await rig.finished()

    @staticmethod
    async def replies(c, *texts):
        """The voice model answers each request with the next text."""
        for count, text in enumerate(texts, 1):
            await c.expect("response.create", count)
            await c.reply(text)

    def asked(self, server) -> list[str]:
        """The words each request put to the voice model."""
        return [e["item"]["content"][0]["text"] for e in server.conn.log
                if e["type"] == "conversation.item.create" and e["item"]["type"] == "message"]

    async def test_the_next_hold_carries_on_the_same_session(self):
        server, rig = await self.rigged(lambda c: self.replies(c, "It is noon.", "Ten past."))
        await self.hold(rig, "what time is it")
        first = rig.ctl.talk.rt
        await self.hold(rig, "and now")
        self.assertEqual(len(server.connections), 1)
        self.assertEqual(len(server.sessions), 1)
        self.assertEqual(self.asked(server), ["what time is it", "and now"])
        self.assertIs(rig.ctl.talk.rt, first)
        self.assertEqual(rig.of("Reply"), ["It is noon.", "Ten past."])
        self.assertEqual(rig.of("Error"), [])
        self.assertEqual(len(rig.history.recent()), 2)

    async def test_the_microphone_opens_only_while_fn_is_held(self):
        _, rig = await self.rigged(lambda c: self.replies(c, "It is noon."))
        await self.hold(rig)
        await asyncio.sleep(0.3)
        self.assertEqual(rig.journal, ["mic.start", "mic.stop", "speaker.start", "speaker.drained"])
        self.assertEqual(len(rig.mics), 1)
        self.assertIsNotNone(rig.ctl.talk)

    async def test_nothing_the_server_sends_between_holds_can_act(self):
        async def script(c):
            await self.replies(c, "It is noon.")
            await asyncio.sleep(0.2)
            await c.tool_call("time_now", "late")
            await c.reply("Unasked.")

        ran = []
        _, rig = await self.rigged(script)
        with mock.patch.dict(skills.HANDLERS, {"time.now": lambda *_: ran.append(1) or {}}):
            await self.hold(rig)
            await asyncio.sleep(0.5)
        self.assertEqual(ran, [])
        self.assertEqual(rig.of("Reply"), ["It is noon."])
        self.assertEqual(rig.speakers[-1:], rig.speakers[:1])
        self.assertTrue(rig.ctl.talk.rt.alive)

    async def test_a_vad_turn_is_not_kept(self):
        async def script(c):
            await c.heard_audio(1)
            await c.committed("what time is it")
            await c.reply()

        server, rig = await self.rigged(script, conversation_mode=False)
        rig.mic_options = {}
        await rig.ctl.toggle()
        await rig.finished()
        self.assertIsNone(rig.ctl.talk)
        await asyncio.wait_for(server.conn.ws.wait_closed(), 2)

    async def test_a_follow_up_still_needs_the_card_the_conversation_earned(self):
        """It read something private in the first hold, so the weather in the second asks."""
        async def script(c):
            await c.expect("response.create")
            await c.tool_call("time_now", "c1")
            await c.done()
            await c.expect("response.create", 2)
            await c.reply("It is noon.")
            await c.expect("response.create", 3)
            await c.tool_call("weather_forecast", "c2")
            await c.done()
            await c.expect("response.create", 4)
            await c.reply("All right, I won't.")

        server, rig = await self.rigged(script)
        with mock.patch.dict(skills.HANDLERS, {"time.now": private}):
            await self.hold(rig, "read my notes")
            self.said.append("and the weather")
            await rig.ctl.hold_start()
            await rig.state("listening")
            await asyncio.sleep(0.15)
            await rig.ctl.hold_end()
            async with asyncio.timeout(5):
                while not rig.of("Approval"):
                    await asyncio.sleep(0.005)
            request, _, detail = rig.of("Approval")[0][:3]
            self.assertIn("already read something private", detail)
            rig.ctl.answer(request, False)
            await rig.finished()
        self.assertEqual(len(server.connections), 1)
        self.assertEqual(rig.of("Reply")[-1], "All right, I won't.")
        # A no ends it: the next hold is a new conversation that has read nothing.
        self.assertIsNone(rig.ctl.talk)
        await asyncio.wait_for(server.conn.ws.wait_closed(), 2)

    async def test_after_a_no_the_next_hold_starts_afresh(self):
        async def script(c):
            if len(server.connections) == 1:
                await c.expect("response.create")
                await c.tool_call("time_now", "c1")
                await c.done()
                await c.expect("response.create", 2)
                await c.reply("I didn't.")
            else:
                await self.replies(c, "It is noon.")

        server, rig = await self.rigged(script)
        with mock.patch.dict(skills.HANDLERS, {"time.now": lambda *_: Ask("Do it?", "d", dict)}):
            self.said.append("do it")
            await rig.ctl.hold_start()
            await rig.state("listening")
            await asyncio.sleep(0.15)
            await rig.ctl.hold_end()
            async with asyncio.timeout(5):
                while not rig.of("Approval"):
                    await asyncio.sleep(0.005)
            rig.ctl.answer(rig.of("Approval")[0][0], False)
            await rig.finished()
        self.assertIsNone(rig.ctl.talk)
        await self.hold(rig)
        self.assertEqual(len(server.connections), 2)
        self.assertFalse(rig.ctl.talk.rt.session.tainted)

    async def test_a_card_that_runs_out_of_time_does_not_end_it(self):
        async def script(c):
            await c.expect("response.create")
            await c.tool_call("time_now", "c1")
            await c.done()
            await c.expect("response.create", 2)
            await c.reply("You didn't answer.")

        server, rig = await self.rigged(script)
        with mock.patch.dict(skills.HANDLERS, {"time.now": lambda *_: Ask("Do it?", "d", dict)}), \
                mock.patch.object(controller, "APPROVAL_WAIT_S", 0.2):
            await self.hold(rig, "do it")
        self.assertIsNotNone(rig.ctl.talk)
        self.assertTrue(rig.ctl.talk.rt.alive)

    async def test_cancel_ends_it(self):
        server, rig = await self.rigged(lambda c: self.replies(c, "It is noon."))
        with mock.patch.dict(skills.HANDLERS, {"time.now": private}):
            await self.hold(rig)
        rig.ctl.talk.rt.session.tainted = True
        await rig.ctl.cancel()
        self.assertIsNone(rig.ctl.talk)
        await asyncio.wait_for(server.conn.ws.wait_closed(), 2)
        await self.hold(rig)
        self.assertEqual(len(server.connections), 2)
        self.assertFalse(rig.ctl.talk.rt.session.tainted)

    async def test_a_lock_ends_it(self):
        server, rig = await self.rigged(lambda c: self.replies(c, "It is noon."))
        await self.hold(rig)
        await rig.ctl.set_locked(True)
        await rig.ctl.set_locked(False)
        self.assertIsNone(rig.ctl.talk)
        await asyncio.wait_for(server.conn.ws.wait_closed(), 2)

    async def test_toggle_ends_it(self):
        server, rig = await self.rigged(lambda c: self.replies(c, "It is noon."))
        await self.hold(rig)
        first = rig.ctl.talk.rt
        await rig.ctl.toggle()
        self.assertIsNone(rig.ctl.talk)
        self.assertFalse(first.alive)
        self.assertIsNot(rig.ctl.turn.rt, first)

    async def test_two_minutes_of_quiet_end_it(self):
        server, rig = await self.rigged(lambda c: self.replies(c, QUESTION))
        with mock.patch.object(controller, "FOLLOW_ON_S", 0.3):
            await self.hold(rig)
            self.assertIsNotNone(rig.ctl.talk)
            await asyncio.sleep(0.1)
            self.assertIsNotNone(rig.ctl.talk, "it ended early")
            await asyncio.wait_for(server.conn.ws.wait_closed(), 2)
        self.assertIsNone(rig.ctl.talk)
        self.assertEqual(rig.of("FollowUp")[-1], ("", "", 0))

    async def test_each_hold_restarts_the_two_minutes(self):
        server, rig = await self.rigged(lambda c: self.replies(c, "One.", "Two."))
        with mock.patch.object(controller, "FOLLOW_ON_S", 0.4):
            await self.hold(rig)
            await asyncio.sleep(0.25)
            await self.hold(rig)
            await asyncio.sleep(0.25)
            self.assertIsNotNone(rig.ctl.talk)
            self.assertTrue(rig.ctl.talk.rt.alive)

    async def test_a_fn_shortcut_while_dictating_leaves_it_alone(self):
        rig = self.rig(1)
        daemon = service.Daemon(rig.config, rig.history, CountingSkills())
        rt = mock.Mock(close=mock.AsyncMock())
        daemon.controller.talk = Talk(rt)
        await daemon.fn_keys().on_dictate_cancel()
        self.assertIsNotNone(daemon.controller.talk)
        rt.close.assert_not_awaited()
        await daemon.controller.cancel()
        self.assertIsNone(daemon.controller.talk)
        rt.close.assert_awaited()

    async def test_a_dropped_connection_comes_back_with_what_the_conversation_read(self):
        server, rig = await self.rigged(lambda c: self.replies(c, "It is noon."))
        await self.hold(rig)
        session = rig.ctl.talk.rt.session
        session.tainted = True
        await server.conn.ws.close()
        await asyncio.sleep(0.1)
        self.assertFalse(rig.ctl.talk.rt.alive)
        await self.hold(rig, "and now")
        self.assertEqual(len(server.connections), 2)
        self.assertEqual(rig.of("Error"), [])
        self.assertIs(rig.ctl.talk.rt.session, session)
        self.assertTrue(session.tainted)
        self.assertEqual(self.asked(server), ["and now"])

    async def test_a_hold_that_cuts_a_reply_short_keeps_the_conversation(self):
        gate = asyncio.Event()

        async def script(c):
            if len(server.connections) == 1:
                await c.expect("response.create")
                await c.reply("A long answer.")
            else:
                await self.replies(c, "Short.")

        server = await self.server(script)
        rig = self.rig(server.port, mic_pcm=VOICE, mic={"block": True}, speaker={"gate": gate})
        self.said.append("tell me a story")
        await rig.ctl.hold_start()
        await rig.state("listening")
        await asyncio.sleep(0.15)
        await rig.ctl.hold_end()
        await rig.state("speaking")
        session = rig.ctl.turn.rt.session
        session.tainted = True
        gate.set()
        rig.speaker_options = {}
        await self.hold(rig, "stop, what time is it")
        self.assertEqual(len(server.connections), 2)
        self.assertIs(rig.ctl.talk.rt.session, session)
        self.assertTrue(session.tainted)

    async def test_reloaded_skills_get_a_new_session_in_the_same_conversation(self):
        server, rig = await self.rigged(lambda c: self.replies(c, "It is noon."))
        await self.hold(rig)
        session = rig.ctl.talk.rt.session
        session.tainted = True
        newer = CountingSkills()
        rig.ctl.host = _Fixed(newer)
        await self.hold(rig, "and now")
        self.assertEqual(len(server.connections), 2)
        self.assertIs(rig.ctl.talk.rt.session, session)
        self.assertIs(session.skills, newer)
        self.assertTrue(session.tainted)

    async def test_a_question_stays_on_screen_with_the_hint(self):
        _, rig = await self.rigged(lambda c: self.replies(c, QUESTION))
        await self.hold(rig, "print my report")
        self.assertEqual(rig.of("FollowUp"), [(QUESTION, "Hold Fn to answer", 10)])
        follow = rig.index("FollowUp", QUESTION, "Hold Fn to answer", 10)
        self.assertGreater(follow, rig.index("StateChanged", "idle"))
        self.assertGreater(follow, rig.index("Presence", False))
        self.assertTrue(rig.ctl.talk.asked)

    async def test_a_plain_reply_has_no_hint(self):
        _, rig = await self.rigged(lambda c: self.replies(c, "It is noon."))
        await self.hold(rig)
        self.assertEqual(rig.of("FollowUp"), [])

    async def test_the_answer_goes_to_the_task_that_asked(self):
        async def script(c):
            await c.expect("response.create")
            await c.tool_call("task_run", "t1", json.dumps({"goal": "print my report"}))
            await c.done()
            await c.expect("response.create", 2)
            await c.reply(QUESTION)

        Model.script = ["I found report-a and report-b. Which one should I print?",
                        "Printed report-b."]
        server, rig = await self.rigged(script)
        await self.hold(rig, "print my report")
        self.assertEqual(rig.of("FollowUp")[0][0], QUESTION)
        await self.hold(rig, "the second one")
        self.assertEqual([m.heard for m in Model.made], [["print my report", "the second one"]])
        self.assertEqual(self.asked(server), ["print my report"], "the voice model was asked")
        self.assertEqual(self.sorted, ["print my report"], "the answer was sorted as a fact")
        self.assertEqual(rig.of("Reply")[-1], "Printed report-b.")
        self.assertEqual(self.speech.await_args.args[1], "Printed report-b.")
        self.assertEqual(rig.of("Error"), [])
        self.assertEqual(rig.history.recent()[0]["heard"], "the second one")
        self.assertEqual([a["name"] for a in rig.history.recent()[0]["actions"]], ["task.run"])

    async def test_a_task_waits_only_while_its_question_is_the_last_word(self):
        async def script(c):
            await c.expect("response.create")
            await c.tool_call("task_run", "t1", json.dumps({"goal": "print my report"}))
            await c.done()
            await c.expect("response.create", 2)
            await c.reply("Let me know.")   # the voice model didn't pass the question on
            await c.expect("response.create", 3)
            await c.reply("It is noon.")

        Model.script = ["Which one should I print?"]
        server, rig = await self.rigged(script)
        await self.hold(rig, "print my report")
        self.assertIsNone(rig.ctl.talk.rt.session.waiting)
        await self.hold(rig, "what time is it")
        self.assertEqual(len(Model.made), 1)
        self.assertEqual(self.asked(server), ["print my report", "what time is it"])

    async def test_a_new_conversation_has_no_task_waiting(self):
        async def script(c):
            if len(server.connections) == 1:
                await c.expect("response.create")
                await c.tool_call("task_run", "t1", json.dumps({"goal": "print my report"}))
                await c.done()
                await c.expect("response.create", 2)
                await c.reply(QUESTION)
            else:
                await self.replies(c, "It is noon.")

        Model.script = ["Which one should I print?"]
        server, rig = await self.rigged(script)
        await self.hold(rig, "print my report")
        await rig.ctl.cancel()
        await self.hold(rig, "the second one")
        self.assertEqual([m.heard for m in Model.made], [["print my report"]])
        self.assertEqual(self.asked(server), ["the second one"])

    async def test_a_new_voice_between_holds_gets_a_new_session(self):
        server, rig = await self.rigged(lambda c: self.replies(c, "It is noon."))
        await self.hold(rig)
        session = rig.ctl.talk.rt.session
        session.tainted = True
        rig.config.set("voice", "am_adam")
        await self.hold(rig, "and now")
        self.assertEqual(len(server.connections), 2)
        self.assertEqual(server.sessions[-1]["audio"]["output"]["voice"], "am_adam")
        self.assertIs(rig.ctl.talk.rt.session, session)
        self.assertTrue(session.tainted, "a new voice must not drop a card the conversation needs")

    async def test_new_instructions_between_holds_get_a_new_session(self):
        server, rig = await self.rigged(lambda c: self.replies(c, "It is noon."))
        await self.hold(rig)
        rig.config.set("instructions", "Answer in one word.")
        await self.hold(rig, "and now")
        self.assertEqual(len(server.connections), 2)
        self.assertEqual(server.sessions[-1]["instructions"], "Answer in one word.")

    async def test_a_new_server_between_holds_gets_the_next_words(self):
        old, rig = await self.rigged(lambda c: self.replies(c, "It is noon."))
        await self.hold(rig)
        new = await self.server(lambda c: self.replies(c, "Ten past."))
        rig.config.set("server", f"127.0.0.1:{new.port}")     # which clears the key
        rig.config.set("api_key", "good")
        await self.hold(rig, "and now")
        self.assertEqual(self.asked(old), ["what time is it"])
        self.assertEqual(self.asked(new), ["and now"])
        self.assertEqual(len(old.connections), 1)
        await asyncio.wait_for(old.conn.ws.wait_closed(), 2)

    async def test_a_new_server_and_no_key_sends_nothing_to_the_old_one(self):
        old, rig = await self.rigged(lambda c: self.replies(c, "It is noon."))
        await self.hold(rig)
        rig.config.set("server", "127.0.0.1:9")   # which clears the key
        self.said.append("and now")
        await rig.ctl.hold_start()
        await asyncio.sleep(0.15)
        await rig.ctl.hold_end()
        await rig.finished()
        self.assertEqual(self.asked(old), ["what time is it"])
        self.assertEqual(len(old.connections), 1)
        self.assertEqual(len(rig.of("Error")), 1)
        await asyncio.wait_for(old.conn.ws.wait_closed(), 2)

    async def test_the_same_settings_keep_the_same_session(self):
        server, rig = await self.rigged(lambda c: self.replies(c, "It is noon.", "Ten past."))
        await self.hold(rig)
        rig.config.set("voice", rig.config["voice"])
        rig.config.set("listen_timeout_s", 9)   # nothing the session was opened with
        await self.hold(rig, "and now")
        self.assertEqual(len(server.connections), 1)

    async def task_asks(self, then: str, one_config=False):
        """A hold whose task asks which report to print, and a server that answers the next
        request put to the voice model with then. With one_config, the skills read the
        controller's Config, as in service.run."""
        async def script(c):
            await c.expect("response.create")
            await c.tool_call("task_run", "t1", json.dumps({"goal": "print my report"}))
            await c.done()
            await c.expect("response.create", 2)
            await c.reply(QUESTION)
            await c.expect("response.create", 3)
            await c.reply(then)

        Model.script = ["I found report-a and report-b. Which one should I print?",
                        "Printed report-b."]
        server, rig = await self.rigged(script)
        if one_config:
            rig.ctl.skills.config = rig.config
        await self.hold(rig, "print my report")
        self.assertEqual(rig.of("FollowUp")[0][0], QUESTION)
        return server, rig

    async def test_a_hold_after_the_hint_has_gone_is_not_the_answer(self):
        with mock.patch.object(controller, "QUESTION_HINT_S", 0.2), \
                mock.patch.object(controller, "ANSWER_GRACE_S", 0.1):
            server, rig = await self.task_asks("It is sunny.")
            await asyncio.sleep(0.4)
            await self.hold(rig, "what's the weather")
        self.assertEqual([m.heard for m in Model.made], [["print my report"]])
        self.assertEqual(self.asked(server), ["print my report", "what's the weather"])
        self.assertEqual(self.sorted, ["print my report", "what's the weather"])
        self.assertEqual(rig.of("Reply")[-1], "It is sunny.")
        self.assertIsNone(rig.ctl.talk.rt.session.waiting)

    async def test_a_hold_while_the_hint_is_up_is_the_answer(self):
        with mock.patch.object(controller, "QUESTION_HINT_S", 0.2), \
                mock.patch.object(controller, "ANSWER_GRACE_S", 5):
            server, rig = await self.task_asks("unused")
            await asyncio.sleep(0.4)    # past the hint, but a hold just begun as it went
            await self.hold(rig, "the second one")
        self.assertEqual([m.heard for m in Model.made], [["print my report", "the second one"]])
        self.assertEqual(self.asked(server), ["print my report"])

    async def test_a_new_server_drops_the_waiting_task(self):
        """The task's history would go with the answer: to a server it never started on."""
        server, rig = await self.task_asks("unused", one_config=True)
        new = await self.server(lambda c: self.replies(c, "Sorry, print what."))
        rig.config.set("server", f"127.0.0.1:{new.port}")     # which clears the key
        rig.config.set("api_key", "good")
        await self.hold(rig, "the second one")
        self.assertEqual([m.heard for m in Model.made], [["print my report"]])
        self.assertEqual(self.asked(new), ["the second one"])
        self.assertEqual(self.sorted, ["print my report", "the second one"])
        self.assertIsNone(rig.ctl.talk.rt.session.waiting)

    async def test_text_the_app_reads_out_raises_no_hint(self):
        async def script(c):
            await c.expect("response.create")
            await c.tool_call("time_now", "c1")
            await c.done()
            await c.expect("response.create", 2)
            await c.reply("Here it is.")

        def mail(_args, _config, session):
            session.after.append(Speak("Bob wrote: are you free on Friday?", "af_nicole", ""))
            return {"done": True}

        _, rig = await self.rigged(script)
        with mock.patch.dict(skills.HANDLERS, {"time.now": mail}):
            await self.hold(rig, "what did Bob write")
        self.assertEqual(rig.of("Reply"), ["Here it is.", "Bob wrote: are you free on Friday?"])
        self.assertEqual(rig.of("FollowUp"), [])
        self.assertFalse(rig.ctl.talk.asked)

    async def test_a_fact_that_ends_in_a_question_mark_raises_no_hint(self):
        async def is_fact(heard, _config):
            return True

        def answer(_args, _config, session):
            session.after.append(Speak("Nobody knows: is it a planet?", "af_nicole", ""))
            return {"app_says": True}

        _, rig = await self.rigged(lambda c: asyncio.sleep(10))
        with mock.patch.object(controller.knowledge, "is_fact", is_fact), \
                mock.patch.dict(skills.HANDLERS, {"knowledge.answer": answer}):
            await self.hold(rig, "is pluto a planet")
        self.assertEqual(rig.of("Reply"), ["Nobody knows: is it a planet?"])
        self.assertEqual(rig.of("FollowUp"), [])

    async def test_a_task_that_asks_again_keeps_the_hint(self):
        with mock.patch.object(controller, "QUESTION_HINT_S", 5):
            Model.script = ["Which one should I print?", "Colour or black and white?"]
            _, rig = await self.task_asks("unused")
            Model.script = ["Colour or black and white?"]
            await self.hold(rig, "the second one")
        self.assertEqual(rig.of("FollowUp")[-1], ("Colour or black and white?",
                                                  "Hold Fn to answer", 5))
        self.assertIsNotNone(rig.ctl.talk.rt.session.waiting)

    async def found_then(self, end):
        """file.find's results from one hold, then end(rig), then a new hold."""
        def find(_args, _config, session):
            session.found = ["/tmp/report-a.txt", "/tmp/report-b.txt"]
            return {"found": 2}

        async def script(c):
            await c.expect("response.create")
            await c.tool_call("time_now", "c1")
            await c.done()
            await c.expect("response.create", 2)
            await c.reply("I found two.")
            for count in (3, 4):
                if len(server.connections) > 1:
                    break
                await c.expect("response.create", count)
                await c.reply("Done.")

        server, rig = await self.rigged(script)
        with mock.patch.dict(skills.HANDLERS, {"time.now": find}):
            await self.hold(rig, "find my reports")
        await end(rig)
        await rig.ctl.hold_start()
        await rig.state("listening")
        found = list(rig.ctl.turn.rt.session.found)
        await rig.ctl.cancel()
        return found

    async def test_a_lock_forgets_what_was_found(self):
        async def lock(rig):
            await rig.ctl.set_locked(True)
            await rig.ctl.set_locked(False)
        self.assertEqual(await self.found_then(lock), [])

    async def test_cancel_forgets_what_was_found(self):
        async def stop(rig):
            await rig.ctl.cancel()
        self.assertEqual(await self.found_then(stop), [])

    async def test_two_quiet_minutes_keep_what_was_found(self):
        async def wait(rig):
            await rig.ctl._end_talk()   # as the two minutes do
        self.assertEqual(await self.found_then(wait), ["/tmp/report-a.txt", "/tmp/report-b.txt"])

    async def test_a_no_forgets_what_was_found(self):
        def find(_args, _config, session):
            session.found = ["/tmp/report-a.txt"]
            return Ask("Do it?", "d", dict)

        async def script(c):
            if len(server.connections) == 1:
                await c.expect("response.create")
                await c.tool_call("time_now", "c1")
                await c.done()
                await c.expect("response.create", 2)
                await c.reply("I didn't.")

        server, rig = await self.rigged(script)
        with mock.patch.dict(skills.HANDLERS, {"time.now": find}):
            self.said.append("find it and do it")
            await rig.ctl.hold_start()
            await rig.state("listening")
            await asyncio.sleep(0.15)
            await rig.ctl.hold_end()
            async with asyncio.timeout(5):
                while not rig.of("Approval"):
                    await asyncio.sleep(0.005)
            rig.ctl.answer(rig.of("Approval")[0][0], False)
            await rig.finished()
        self.assertEqual(rig.ctl.found, [])
        await rig.ctl.hold_start()
        await rig.state("listening")
        self.assertEqual(rig.ctl.turn.rt.session.found, [])
        await rig.ctl.cancel()
