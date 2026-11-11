"""The planner: the step loop, its two models and what reaches the cloud.

Both models are fakes: realtime._request for the local one, a stand-in anthropic module for the
cloud. Nothing here touches the network or a real key.
"""
import asyncio
import copy
import importlib.util
import inspect
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace as Fake
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tuxistant import planner, realtime  # noqa: E402
from tuxistant import skills as skills_module  # noqa: E402
from tuxistant.config import Config  # noqa: E402
from tuxistant.gate import APP_SAYS, MAX_DETAIL_CHARS, Ask, Session, Speak, printable  # noqa: E402
from tuxistant.realtime import AuthError, Realtime, RealtimeError  # noqa: E402
from tuxistant.skills import BUILTIN, Skills  # noqa: E402

KEY = "sk-fake-test-key-0123456789"
MANIFEST = """
id: org.test.plan
name: Plan
description: d
permissions: {}
actions:
  plan.look:
    description: Look at something
    params: { what: { type: string } }
  plan.send: { description: Send something }
"""
NETWORK = """
id: org.test.net
name: Net
description: d
permissions: { network: [evil.example.com] }
actions:
  plan.leak: { description: Reach out }
"""
SDK_ON_DISK = """
from types import SimpleNamespace as Fake
class AuthenticationError(Exception): pass
PermissionDeniedError = NotFoundError = RateLimitError = APIConnectionError = AuthenticationError
class Anthropic:
    def __init__(self, **_):
        reply = Fake(stop_reason="end_turn", content=[Fake(type="text", text="From the folder.")])
        self.beta = Fake(messages=Fake(create=lambda **_: reply))
"""


def said(text):
    return {"choices": [{"message": {"role": "assistant", "content": text}}]}


def asks(*calls):
    """The model wants these (name, arguments) run. LocalAI sends content as an empty string."""
    return {"choices": [{"message": {"role": "assistant", "content": "", "tool_calls": [
        {"id": f"id{index}", "type": "function", "function": {"name": name, "arguments": arguments}}
        for index, (name, arguments) in enumerate(calls)]}}]}


class Server:
    """Stands in for realtime._request: plays back replies and keeps what it was sent."""

    def __init__(self, *replies):
        self.replies, self.bodies, self.threads = list(replies), [], []

    def __call__(self, _config, path, body=None):
        self.path = path
        self.bodies.append(copy.deepcopy(body))
        self.threads.append(threading.current_thread())
        reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        if isinstance(reply, Exception):
            raise reply
        return reply if isinstance(reply, bytes) else json.dumps(reply).encode()


class Clicks:
    def __init__(self, *answers):
        self.answers, self.asked = list(answers), []

    async def __call__(self, ask):
        self.asked.append(ask)
        return self.answers.pop(0) if self.answers else False


class Case(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        manifests = self.folder / "skills"
        manifests.mkdir()
        (manifests / "plan.yaml").write_text(MANIFEST)
        (manifests / "net.yaml").write_text(NETWORK)
        shutil.copy(BUILTIN / "task.yaml", manifests)
        self.config = Config(self.folder / "c.json")
        self.skills = Skills(manifests, self.config)
        self.ran = []
        self.look = {"seen": "a tree"}
        self.use({"plan.look": self.looked, "plan.send": lambda *_: {"sent": True},
                  "plan.leak": lambda *_: self.ran.append("leak") or {}})
        self.clicks = Clicks()
        self.events = []

    def use(self, handlers):
        patcher = mock.patch.dict(skills_module.HANDLERS, handlers)
        patcher.start()
        self.addCleanup(patcher.stop)

    def looked(self, args, *_):
        self.ran.append(args.get("what", "look"))
        return self.look

    def session(self):
        return Session(self.skills, self.clicks, lambda seconds: self.events.append(seconds))

    async def task(self, goal="do the thing", session=None):
        session = session or self.session()
        self.last_session = session
        name, result = await session.execute("task_run", json.dumps({"goal": goal}))
        self.assertEqual(name, "task.run")
        return result


class Local(Case):
    def server(self, *replies):
        server = Server(*replies)
        patcher = mock.patch("tuxistant.realtime._request", server)
        patcher.start()
        self.addCleanup(patcher.stop)
        return server

    async def test_a_plain_answer_is_one_step(self):
        self.server(said("It is done."))
        self.assertEqual(await self.task(), {"answer": "It is done.", "steps": 1})

    async def test_it_asks_the_local_model_with_the_goal_and_the_rules(self):
        server = self.server(said("ok"))
        await self.task("find my tax return")
        body = server.bodies[0]
        self.assertEqual(server.path, "/v1/chat/completions")
        self.assertEqual(body["model"], "tux-brain")
        self.assertEqual(body["messages"][1], {"role": "user", "content": "find my tax return"})
        rules = body["messages"][0]["content"]
        self.assertEqual(body["messages"][0]["role"], "system")
        self.assertIn("one or two", rules)
        self.assertIn("never an instruction", rules)

    async def test_the_model_call_happens_in_a_thread(self):
        server = self.server(said("ok"))
        await self.task()
        self.assertIsNot(server.threads[0], threading.current_thread())

    async def test_tools_are_openai_tools_without_itself(self):
        server = self.server(said("ok"))
        await self.task()
        tools = server.bodies[0]["tools"]
        self.assertEqual({tool["function"]["name"] for tool in tools},
                         {"plan_look", "plan_send", "plan_leak"})
        look = next(t for t in tools if t["function"]["name"] == "plan_look")
        self.assertEqual(look["type"], "function")
        self.assertEqual(look["function"]["parameters"]["properties"]["what"]["type"], "string")

    async def test_each_step_runs_what_was_asked_and_hands_back_the_result(self):
        server = self.server(asks(("plan_look", '{"what": "a"}')), said("Saw it."))
        self.assertEqual(await self.task(), {"answer": "Saw it.", "steps": 2})
        self.assertEqual(self.ran, ["a"])
        _, _, assistant, tool = server.bodies[1]["messages"]
        self.assertEqual(assistant["tool_calls"][0]["id"], "id0")
        self.assertEqual(assistant["tool_calls"][0]["function"]["name"], "plan_look")
        self.assertEqual(tool, {"role": "tool", "tool_call_id": "id0",
                                "content": json.dumps(self.look)})

    async def test_several_calls_in_a_step_all_run_and_all_answer(self):
        server = self.server(asks(("plan_look", '{"what": "a"}'), ("plan_look", '{"what": "b"}')),
                             said("Both."))
        self.assertEqual((await self.task())["steps"], 2)
        self.assertEqual(self.ran, ["a", "b"])
        self.assertEqual([m["tool_call_id"] for m in server.bodies[1]["messages"][3:]],
                         ["id0", "id1"])

    async def test_every_model_call_and_action_is_covered_by_busy(self):
        order = []
        real = self.looked
        self.use({"plan.look": lambda *args: order.append("action") or real(*args)})
        replies = [asks(("plan_look", "{}")), said("ok")]

        def request(*_):
            order.append("model")
            return json.dumps(replies.pop(0)).encode()
        with mock.patch("tuxistant.realtime._request", request):
            session = Session(self.skills, self.clicks, lambda s: order.append(("busy", s)))
            await self.task(session=session)
        self.assertEqual(order, [("busy", planner.LOCAL_WAIT_S), "model",
                                 ("busy", planner.ACTION_WAIT_S), "action",
                                 ("busy", planner.LOCAL_WAIT_S), "model"])
        self.assertGreater(planner.LOCAL_WAIT_S, realtime.HTTP_TIMEOUT_S)

    async def test_it_stops_at_eight_steps(self):
        server = self.server(asks(("plan_look", "{}")))
        self.assertEqual(await self.task(), {"answer": planner.OUT_OF_STEPS, "steps": 8})
        self.assertEqual(len(server.bodies), 8)
        self.assertEqual(len(self.ran), 7)      # the last step's call would never be read

    async def test_the_last_step_has_no_tools_and_says_so(self):
        server = self.server(*[asks(("plan_look", "{}"))] * 7, said("Half of it is done."))
        self.assertEqual(await self.task(), {"answer": "Half of it is done.", "steps": 8})
        self.assertNotIn("tools", server.bodies[7])
        self.assertTrue(all("tools" in body for body in server.bodies[:7]))
        self.assertEqual(server.bodies[7]["messages"][-1],
                         {"role": "user", "content": planner.LAST_STEP})

    async def test_a_no_stops_everything_at_once(self):
        ran = []
        self.use({"plan.send": lambda *_: Ask("Send it?", "d", lambda: ran.append(1) or {})})
        server = self.server(asks(("plan_send", "{}"), ("plan_look", "{}")), said("Sent!"))
        self.clicks.answers = [False]
        self.assertEqual(await self.task(), {"answer": planner.STOPPED, "steps": 1})
        self.assertEqual(ran, [])
        self.assertEqual(self.ran, [])
        self.assertEqual(len(server.bodies), 1)     # no chance to ask again, or to claim it worked
        self.assertEqual(len(self.clicks.asked), 1)

    async def test_a_yes_runs_the_action_and_the_task_goes_on(self):
        ran = []
        self.use({"plan.send": lambda *_: Ask("Send it?", "d", lambda: ran.append(1) or {"ok": 1})})
        self.server(asks(("plan_send", "{}")), said("Sent."))
        self.clicks.answers = [True]
        self.assertEqual(await self.task(), {"answer": "Sent.", "steps": 2})
        self.assertEqual(ran, [1])

    async def test_a_step_the_app_reads_out_ends_the_task_with_no_words_of_a_models(self):
        """Shown only "done", the planner's model wrote an answer of its own, the voice model
        was asked to say it, and the app then read out the real one."""
        def answered(_args, _config, session):
            session.after.append(Speak("Astatine.", "voice"))
            return {"done": True, APP_SAYS: True}
        self.use({"plan.look": answered})
        server = self.server(asks(("plan_look", "{}"), ("plan_look", "{}")),
                             said("The rarest element is francium."))
        result = await self.task()
        self.assertEqual(result, {"done": True, APP_SAYS: True, "steps": 1})
        self.assertEqual(len(server.bodies), 1)
        self.assertEqual([item.text for item in self.last_session.take_after()], ["Astatine."])

    def reads_out(self):
        def answered(_args, _config, session):
            session.after.append(Speak("Astatine.", "voice"))
            return {"done": True, APP_SAYS: True}
        self.use({"plan.look": answered, "plan.send": lambda *_: self.ran.append("sent") or {"ok": 1}})

    async def test_a_different_question_in_the_same_reply_is_answered_too(self):
        self.reads_out()
        self.server(asks(("plan_look", "{}"), ("plan_look", '{"what": "other"}')))
        self.assertEqual((await self.task())[APP_SAYS], True)
        self.assertEqual(len(self.last_session.take_after()), 2)

    async def test_a_call_after_one_the_app_reads_out_still_runs(self):
        """The task used to end at the first such call, and the rest of the reply never ran."""
        self.reads_out()
        server = self.server(asks(("plan_look", "{}"), ("plan_send", "{}")), said("Made up."))
        self.assertEqual(await self.task(), {"answer": planner.REST_DONE, "steps": 1})
        self.assertEqual(self.ran, ["sent"])
        self.assertEqual(len(server.bodies), 1)
        self.assertEqual([item.text for item in self.last_session.take_after()], ["Astatine."])

    async def test_what_an_earlier_step_did_is_not_passed_over_in_silence(self):
        self.reads_out()
        self.server(asks(("plan_send", "{}")), asks(("plan_look", "{}")), said("Made up."))
        self.assertEqual(await self.task(), {"answer": planner.REST_DONE, "steps": 2})

    async def test_another_step_that_failed_is_said_beside_the_apps_answer(self):
        self.reads_out()
        self.use({"plan.send": lambda *_: {"error": "The phone is off."}})
        self.server(asks(("plan_send", "{}"), ("plan_look", "{}")), said("Made up."))
        self.assertEqual(await self.task(), {"answer": planner.PART_FAILED, "steps": 1})
        self.assertEqual(len(self.last_session.take_after()), 1)

    async def test_the_taint_gate_still_applies_inside_a_task(self):
        self.server(asks(("plan_leak", "{}")), said("Leaked."))
        session = self.session()
        session.tainted = True
        self.assertEqual((await self.task(session=session))["answer"], planner.STOPPED)
        self.assertEqual(self.ran, [])
        self.assertIn("evil.example.com", self.clicks.asked[0].title)

    async def test_a_task_counts_as_having_read_something_private(self):
        self.server(said("ok"))
        await self.task()
        self.assertTrue(self.last_session.tainted)

    async def test_a_failed_action_is_data_the_model_can_work_with(self):
        def boom(*_):
            raise OSError("no such file")
        self.use({"plan.look": boom})
        server = self.server(asks(("plan_look", "{}")), said("That file is gone."))
        self.assertEqual((await self.task())["answer"], "That file is gone.")
        self.assertEqual(json.loads(server.bodies[1]["messages"][3]["content"]),
                         {"error": "no such file"})

    async def test_what_a_small_model_gets_wrong(self):
        for name, arguments in (("no_such_tool", "{}"), ("plan_look", "{not json"),
                                ("plan_look", "[1, 2]")):
            with self.subTest(name=name, arguments=arguments):
                server = self.server(asks((name, arguments)), said("Sorry."))
                self.assertEqual(await self.task(), {"answer": "Sorry.", "steps": 2})
                self.assertIn("error", json.loads(server.bodies[1]["messages"][3]["content"]))

    async def test_a_dotted_name_still_works(self):
        self.server(asks(("plan.look", '{"what": "a"}')), said("ok"))
        await self.task()
        self.assertEqual(self.ran, ["a"])

    async def test_arguments_as_an_object_and_a_missing_id(self):
        reply = {"choices": [{"message": {"tool_calls": [
            {"function": {"name": "plan_look", "arguments": {"what": "a"}}}]}}]}
        server = self.server(reply, said("ok"))
        await self.task()
        self.assertEqual(self.ran, ["a"])
        self.assertEqual(server.bodies[1]["messages"][3]["tool_call_id"], "call_0")

    async def test_it_can_never_start_itself(self):
        for name in ("task_run", "task.run"):
            with self.subTest(name):
                server = self.server(asks((name, '{"goal": "again"}')), said("ok"))
                self.assertEqual((await self.task())["steps"], 2)
                self.assertEqual(len(server.bodies), 2)
                self.assertIn("error", json.loads(server.bodies[1]["messages"][3]["content"]))

    async def test_an_action_that_returns_no_dict_does_not_lose_the_task(self):
        self.use({"plan.send": lambda *_: Ask("Send it?", "d", lambda: None)})
        server = self.server(asks(("plan_send", "{}")), said("Sent."))
        self.clicks.answers = [True]
        self.assertEqual(await self.task(), {"answer": "Sent.", "steps": 2})
        self.assertEqual(json.loads(server.bodies[1]["messages"][3]["content"]), {"result": None})

    async def test_a_result_json_cannot_hold_still_reaches_the_model(self):
        self.look = {"seen": Path("/some/where")}
        server = self.server(asks(("plan_look", "{}")), said("ok"))
        self.assertEqual((await self.task())["answer"], "ok")
        self.assertIn("/some/where", server.bodies[1]["messages"][3]["content"])

    async def test_a_long_result_is_cut_and_the_cut_is_marked(self):
        self.look = {"seen": "x" * 20000}
        server = self.server(asks(("plan_look", "{}")), said("ok"))
        await self.task()
        sent = server.bodies[1]["messages"][3]["content"]
        self.assertLessEqual(len(sent), 4000)
        self.assertIn("more characters", sent)

    async def test_a_result_cannot_smuggle_control_characters(self):
        self.look = {"seen": "ok\x1b[2J‮"}
        server = self.server(asks(("plan_look", "{}")), said("ok"))
        await self.task()
        sent = server.bodies[1]["messages"][3]["content"]
        self.assertNotIn("\x1b", sent)
        self.assertNotIn("‮", sent)

    async def test_file_contents_stay_home_without_a_card(self):
        self.look = {"output": "secret", "file_contents": True}
        server = self.server(asks(("plan_look", "{}")), said("ok"))
        await self.task()
        self.assertIn("secret", server.bodies[1]["messages"][3]["content"])
        self.assertEqual(self.clicks.asked, [])

    async def test_failures_to_reach_the_model_are_sentences(self):
        for error, heard in ((RealtimeError("Cannot reach the server at stormshadow:8080"),
                              "Cannot reach the server at stormshadow:8080."),
                             (AuthError(), "The server rejected the API key."),
                             (RealtimeError("Server error: HTTP 500"), "Server error: HTTP 500.")):
            with self.subTest(heard):
                self.server(error)
                self.assertEqual(await self.task(), {"answer": heard, "steps": 1})

    async def test_a_failure_after_an_action_says_it_may_be_part_done(self):
        self.server(asks(("plan_look", "{}")), RealtimeError("Lost it"))
        self.assertEqual(await self.task(),
                         {"answer": f"Lost it. {planner.PART_DONE}", "steps": 2})

    async def test_a_failure_after_only_failed_actions_does_not(self):
        self.server(asks(("no_such_tool", "{}")), RealtimeError("Lost it"))
        self.assertEqual((await self.task())["answer"], "Lost it.")

    async def test_a_reply_that_makes_no_sense_is_a_sentence(self):
        for reply in (b"not json", {}, [1], {"choices": []}, {"choices": [{"message": None}]},
                      {"choices": [{"message": {"tool_calls": [{}]}}]},
                      {"choices": [{"message": {"tool_calls": ["x"]}}]}):
            with self.subTest(reply):
                self.server(reply)
                self.assertEqual((await self.task())["answer"], planner.UNREADABLE)

    async def test_no_words_and_no_calls(self):
        for reply in (said(""), said(None), said("   \n ")):
            with self.subTest(reply):
                self.server(reply)
                self.assertEqual(await self.task(), {"answer": planner.NO_ANSWER, "steps": 1})

    async def test_a_rambling_answer_is_cut_to_two_sentences(self):
        self.server(said("You have 3.5 GB free. It is noon! Also this. And that."))
        self.assertEqual((await self.task())["answer"], "You have 3.5 GB free. It is noon!")

    async def test_an_empty_goal_never_reaches_a_model(self):
        server = self.server(said("ok"))
        for arguments in ('{}', '{"goal": ""}', '{"goal": "  \\n "}', '{"goal": null}'):
            with self.subTest(arguments):
                result = (await self.session().execute("task_run", arguments))[1]
                self.assertEqual(result, {"answer": planner.NOT_CLEAR, "steps": 0})
        self.assertEqual(server.bodies, [])

    async def test_a_runaway_goal_is_cut_and_tidied(self):
        server = self.server(said("ok"))
        await self.task("line one\n\tline two " + "x" * 5000)
        goal = server.bodies[0]["messages"][1]["content"]
        self.assertTrue(goal.startswith("line one line two x"))
        self.assertEqual(len(goal), planner.MAX_GOAL_CHARS)

    async def test_a_goal_that_is_not_text_does_no_harm(self):
        self.server(said("ok"))
        result = (await self.session().execute("task_run", '{"goal": {"a": 1}}'))[1]
        self.assertEqual(result["steps"], 1)

    async def test_the_cloud_is_not_touched(self):
        self.server(said("ok"))
        with mock.patch.dict(sys.modules, {"anthropic": None}):
            self.assertEqual((await self.task())["answer"], "ok")


class Errors:
    """The SDK's exceptions, related as the real ones are."""

    class AnthropicError(Exception): pass
    class APIError(AnthropicError): pass
    class APIStatusError(APIError): pass
    class APIConnectionError(APIError): pass
    class APITimeoutError(APIConnectionError): pass
    class BadRequestError(APIStatusError): pass
    class AuthenticationError(APIStatusError): pass
    class PermissionDeniedError(APIStatusError): pass
    class NotFoundError(APIStatusError): pass
    class RateLimitError(APIStatusError): pass
    class InternalServerError(APIStatusError): pass


def text(words):
    return Fake(type="text", text=words)


def tool_use(id, name, **arguments):
    return Fake(type="tool_use", id=id, name=name, input=arguments)


def reply(*content, stop_reason="end_turn"):
    return Fake(stop_reason=stop_reason, content=list(content))


def calls(*blocks):
    return reply(*blocks, stop_reason="tool_use")


class FakeSdk(Errors):
    def __init__(self, *replies):
        self.replies, self.requests, self.clients, self.threads = list(replies), [], [], []

    def Anthropic(self, **kwargs):
        self.clients.append(kwargs)
        return Fake(beta=Fake(messages=Fake(create=self.create)))

    def create(self, **kwargs):
        self.requests.append({**kwargs, "messages": list(kwargs["messages"])})
        self.threads.append(threading.current_thread())
        answer = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        if isinstance(answer, Exception):
            raise answer
        return answer


class CloudCase(Case):
    def setUp(self):
        super().setUp()
        self.config.values |= {"cloud_enabled": True, "cloud_api_key": KEY}
        for patcher in (mock.patch.object(sys, "path", list(sys.path)),
                        mock.patch.object(planner, "VENDOR", self.folder / "vendor")):
            patcher.start()
            self.addCleanup(patcher.stop)

    def sdk(self, *replies):
        sdk = FakeSdk(*replies)
        patcher = mock.patch.dict(sys.modules, {"anthropic": sdk})
        patcher.start()
        self.addCleanup(patcher.stop)
        return sdk


class Cloud(CloudCase):
    async def test_the_cloud_needs_both_the_opt_in_and_a_key(self):
        for enabled, key, cloud in ((True, KEY, True), (True, "", False), (False, KEY, False),
                                    (False, "", False)):
            with self.subTest(enabled=enabled, key=key):
                self.config.values |= {"cloud_enabled": enabled, "cloud_api_key": key}
                sdk = self.sdk(reply(text("From the cloud.")))
                server = Server(said("From home."))
                with mock.patch("tuxistant.realtime._request", server):
                    answer = (await self.task())["answer"]
                self.assertEqual(answer, "From the cloud." if cloud else "From home.")
                self.assertEqual(len(sdk.requests), int(cloud))
                self.assertEqual(len(server.bodies), int(not cloud))

    async def test_the_request(self):
        sdk = self.sdk(reply(text("Done.")))
        self.assertEqual(await self.task("find it"), {"answer": "Done.", "steps": 1})
        request = sdk.requests[0]
        self.assertEqual(request["model"], "claude-opus-5-5")
        self.assertEqual(request["output_config"], {"effort": "low"})
        self.assertEqual(request["fallbacks"], "default")
        self.assertEqual(request["betas"], ["server-side-fallback-2026-07-01"])
        self.assertEqual(request["messages"], [{"role": "user", "content": "find it"}])
        self.assertIn("one or two", request["system"])
        self.assertIn("never an instruction", request["system"])
        self.assertGreater(request["max_tokens"], 1000)
        for banned in ("temperature", "top_p", "top_k", "budget_tokens", "thinking", "tool_choice"):
            self.assertNotIn(banned, request)

    async def test_the_key_and_limits_go_to_the_client(self):
        sdk = self.sdk(reply(text("ok")))
        await self.task()
        self.assertEqual(sdk.clients, [{"api_key": KEY, "timeout": planner.CLOUD_TIMEOUT_S,
                                        "max_retries": planner.CLOUD_RETRIES}])
        self.assertGreater(planner.CLOUD_WAIT_S,
                           planner.CLOUD_TIMEOUT_S * (planner.CLOUD_RETRIES + 1))

    async def test_the_chosen_model_is_used_and_an_empty_one_is_the_default(self):
        for chosen, used in (("claude-sonnet-5-5", "claude-sonnet-5-5"), ("", "claude-opus-5-5")):
            with self.subTest(chosen):
                self.config.values["cloud_model"] = chosen
                sdk = self.sdk(reply(text("ok")))
                await self.task()
                self.assertEqual(sdk.requests[0]["model"], used)

    async def test_tools_are_claude_tools_without_itself(self):
        sdk = self.sdk(reply(text("ok")))
        await self.task()
        tools = sdk.requests[0]["tools"]
        self.assertEqual({tool["name"] for tool in tools}, {"plan_look", "plan_send", "plan_leak"})
        look = next(tool for tool in tools if tool["name"] == "plan_look")
        self.assertEqual(look, {"name": "plan_look", "description": "Look at something",
                                "input_schema": {"type": "object", "required": [],
                                                 "properties": {"what": {"type": "string"}}}})

    async def test_system_and_tools_do_not_change_between_steps(self):
        sdk = self.sdk(calls(tool_use("t1", "plan_look")), reply(text("ok")))
        await self.task()
        self.assertEqual(sdk.requests[0]["system"], sdk.requests[1]["system"])
        self.assertEqual(sdk.requests[0]["tools"], sdk.requests[1]["tools"])

    async def test_it_runs_in_a_thread_with_one_client(self):
        sdk = self.sdk(calls(tool_use("t1", "plan_look")), reply(text("ok")))
        await self.task()
        self.assertIsNot(sdk.threads[0], threading.current_thread())
        self.assertEqual(len(sdk.clients), 1)

    async def test_the_reply_goes_back_unchanged_with_its_thinking(self):
        thinking = Fake(type="thinking", thinking="", signature="sig")
        first = calls(thinking, text("Looking."), tool_use("t1", "plan_look", what="a"))
        sdk = self.sdk(first, reply(text("ok")))
        await self.task()
        self.assertEqual(sdk.requests[1]["messages"][1]["role"], "assistant")
        self.assertIs(sdk.requests[1]["messages"][1]["content"], first.content)
        self.assertEqual(self.ran, ["a"])

    async def test_earlier_messages_are_never_edited(self):
        sdk = self.sdk(calls(tool_use("t1", "plan_look")), calls(tool_use("t2", "plan_look")),
                       reply(text("ok")))
        await self.task()
        first, second, third = (request["messages"] for request in sdk.requests)
        self.assertEqual(second[:len(first)], first)
        self.assertEqual(third[:len(second)], second)

    async def test_all_results_of_a_step_go_in_one_user_message(self):
        sdk = self.sdk(calls(tool_use("t1", "plan_look", what="a"),
                             tool_use("t2", "plan_look", what="b")), reply(text("ok")))
        await self.task()
        messages = sdk.requests[1]["messages"]
        self.assertEqual([m["role"] for m in messages], ["user", "assistant", "user"])
        self.assertEqual([(b["type"], b["tool_use_id"]) for b in messages[2]["content"]],
                         [("tool_result", "t1"), ("tool_result", "t2")])

    async def test_a_failed_action_is_marked_as_an_error(self):
        sdk = self.sdk(calls(tool_use("t1", "no_such_tool"), tool_use("t2", "plan_look")),
                       reply(text("ok")))
        await self.task()
        failed, fine = sdk.requests[1]["messages"][2]["content"]
        self.assertIs(failed["is_error"], True)
        self.assertIn("error", json.loads(failed["content"]))
        self.assertNotIn("is_error", fine)

    async def test_the_last_step_says_so_and_a_call_on_it_does_not_run(self):
        sdk = self.sdk(calls(tool_use("t1", "plan_look")))
        self.assertEqual(await self.task(), {"answer": planner.OUT_OF_STEPS, "steps": 8})
        self.assertEqual(len(sdk.requests), 8)
        self.assertEqual(len(self.ran), 7)
        self.assertEqual(sdk.requests[7]["messages"][-1]["content"][-1],
                         {"type": "text", "text": planner.LAST_STEP})
        self.assertNotIn("tool_choice", sdk.requests[7])
        self.assertEqual(sdk.requests[7]["tools"], sdk.requests[0]["tools"])

    async def test_a_no_stops_the_cloud_task_too(self):
        self.use({"plan.send": lambda *_: Ask("Send it?", "d", dict)})
        sdk = self.sdk(calls(tool_use("t1", "plan_send")), reply(text("Sent!")))
        self.clicks.answers = [False]
        self.assertEqual(await self.task(), {"answer": planner.STOPPED, "steps": 1})
        self.assertEqual(len(sdk.requests), 1)

    async def test_stop_reasons_come_before_the_content(self):
        half = tool_use("t1", "plan_look", what="half a comm")
        for stop_reason, heard in (("refusal", planner.REFUSED), ("max_tokens", planner.CUT_OFF),
                                   ("pause_turn", planner.CLOUD_TROUBLE),
                                   ("model_context_window_exceeded", planner.CLOUD_TROUBLE)):
            with self.subTest(stop_reason):
                self.sdk(reply(half, stop_reason=stop_reason))
                self.assertEqual(await self.task(), {"answer": heard, "steps": 1})
                self.assertEqual(self.ran, [])

    async def test_a_refusal_with_nothing_in_it(self):
        self.sdk(reply(stop_reason="refusal"))
        self.assertEqual((await self.task())["answer"], planner.REFUSED)

    async def test_a_fallback_marker_is_kept_and_not_read(self):
        marker = Fake(type="fallback")
        sdk = self.sdk(calls(marker, tool_use("t1", "plan_look")), reply(marker, text("ok")))
        self.assertEqual((await self.task())["answer"], "ok")
        self.assertIs(sdk.requests[1]["messages"][1]["content"][0], marker)

    async def test_every_api_failure_is_a_sentence_without_the_key(self):
        for error, heard in (
                (Errors.AuthenticationError(f"invalid x-api-key {KEY}"), planner.REJECTED),
                (Errors.PermissionDeniedError(KEY), planner.NOT_ALLOWED),
                (Errors.NotFoundError(KEY), planner.NO_MODEL),
                (Errors.RateLimitError(KEY), planner.RATE_LIMITED),
                (Errors.APIConnectionError(KEY), planner.UNREACHABLE),
                (Errors.APITimeoutError(KEY), planner.UNREACHABLE),
                (Errors.BadRequestError(KEY), planner.CLOUD_TROUBLE),
                (Errors.InternalServerError(KEY), planner.CLOUD_TROUBLE),
                (Errors.APIStatusError(KEY), planner.CLOUD_TROUBLE),
                (Errors.APIError(KEY), planner.CLOUD_TROUBLE),
                (ValueError(f"Illegal header value {KEY}"), planner.CLOUD_TROUBLE)):
            with self.subTest(type(error).__name__):
                self.sdk(error)
                result = await self.task()
                self.assertEqual(result, {"answer": heard, "steps": 1})
                self.assertNotIn(KEY, json.dumps(result))

    async def test_a_401_says_what_the_user_was_promised(self):
        self.sdk(Errors.AuthenticationError("no"))
        self.assertEqual((await self.task())["answer"], "The cloud key was rejected.")

    async def test_a_failure_leaves_no_trace_of_the_cause(self):
        """Skills.execute speaks str(e), and a chained cause would reach the log."""
        self.sdk(Errors.AuthenticationError(KEY))
        model = planner.Cloud(self.config, "goal", [])
        with self.assertRaises(RealtimeError) as caught:
            await model.step([], False)
        self.assertNotIn(KEY, repr(caught.exception))
        self.assertIsNone(caught.exception.__cause__)
        self.assertTrue(caught.exception.__suppress_context__)

    async def test_a_key_that_cannot_be_a_header_never_reaches_the_sdk(self):
        self.config.values["cloud_api_key"] = "bad\nkey"
        sdk = self.sdk(reply(text("ok")))
        self.assertEqual((await self.task())["answer"], planner.BAD_KEY)
        self.assertEqual(sdk.clients, [])

    async def test_a_failure_after_an_action_says_it_may_be_part_done(self):
        self.sdk(calls(tool_use("t1", "plan_look")), Errors.RateLimitError("slow"))
        self.assertEqual((await self.task())["answer"],
                         f"{planner.RATE_LIMITED} {planner.PART_DONE}")

    async def test_busy_covers_the_retries(self):
        self.sdk(calls(tool_use("t1", "plan_look")), reply(text("ok")))
        await self.task()
        self.assertEqual(self.events[0], planner.CLOUD_WAIT_S)
        self.assertEqual(self.events[1], planner.ACTION_WAIT_S)
        self.assertEqual(self.events[2], planner.CLOUD_WAIT_S)


class Privacy(CloudCase):
    SECRET = "my bank password is hunter2"

    def setUp(self):
        super().setUp()
        self.look = {"output": self.SECRET, "file_contents": True}

    def sent(self, sdk):
        return sdk.requests[1]["messages"][2]["content"][0]["content"]

    async def test_home_and_stored_reminders_need_output_approval(self):
        for action in ("home.set", "home.status", "timer.list", "timer.cancel"):
            for field in ("status", "error"):
                with self.subTest(action=action, field=field):
                    tool = action.replace(".", "_")
                    self.skills.actions[action] = self.skills.actions["plan.look"]
                    self.skills.tool_names[tool] = action
                    self.use({action: self.looked})
                    self.look = {field: "Nursery door is unlocked"}
                    sdk = self.sdk(calls(tool_use("t1", tool)), reply(text("ok")))
                    self.clicks.answers = [False]
                    await self.task()
                    self.assertEqual(self.sent(sdk), planner.WITHHELD)
                    self.assertNotIn("Nursery", repr(sdk.requests))
                    self.assertIn("Nursery", self.clicks.asked[-1].detail)

    async def test_a_yes_sends_exactly_what_the_card_showed(self):
        sdk = self.sdk(calls(tool_use("t1", "plan_look")), reply(text("ok")))
        self.clicks.answers = [True]
        await self.task()
        ask = self.clicks.asked[0]
        self.assertEqual(ask.title, "Send this output to Claude?")
        self.assertIn(self.SECRET, ask.detail)
        self.assertEqual(self.sent(sdk), ask.detail)

    async def test_a_no_sends_a_note_and_the_task_goes_on(self):
        sdk = self.sdk(calls(tool_use("t1", "plan_look")), reply(text("I could not read it.")))
        self.clicks.answers = [False]
        self.assertEqual(await self.task(), {"answer": "I could not read it.", "steps": 2})
        self.assertEqual(self.sent(sdk), planner.WITHHELD)
        self.assertNotIn("hunter2", repr(sdk.requests))

    async def test_nobody_there_to_click_is_a_no(self):
        sdk = self.sdk(calls(tool_use("t1", "plan_look")), reply(text("ok")))
        await self.task(session=Session(self.skills))
        self.assertNotIn("hunter2", repr(sdk.requests))

    async def test_a_long_output_is_cut_before_the_card_so_the_card_is_all_of_it(self):
        self.look = {"output": "é‮" * 9000, "file_contents": True}
        sdk = self.sdk(calls(tool_use("t1", "plan_look")), reply(text("ok")))
        self.clicks.answers = [True]
        await self.task()
        detail = self.clicks.asked[0].detail
        self.assertEqual(self.sent(sdk), detail)
        self.assertLessEqual(len(detail), MAX_DETAIL_CHARS)
        self.assertEqual(printable(detail, MAX_DETAIL_CHARS), detail)   # what the controller shows
        self.assertIn("more characters", detail)

    async def test_each_marked_result_is_asked_about_on_its_own(self):
        sdk = self.sdk(calls(tool_use("t1", "plan_look"), tool_use("t2", "plan_look")),
                       reply(text("ok")))
        self.clicks.answers = [True, False]
        await self.task()
        allowed, withheld = sdk.requests[1]["messages"][2]["content"]
        self.assertIn("hunter2", allowed["content"])
        self.assertEqual(withheld["content"], planner.WITHHELD)
        self.assertEqual(len(self.clicks.asked), 2)

    async def test_unmarked_results_need_no_card(self):
        self.look = {"names": ["tax-2024.pdf"]}
        sdk = self.sdk(calls(tool_use("t1", "plan_look")), reply(text("ok")))
        await self.task()
        self.assertEqual(self.clicks.asked, [])
        self.assertIn("tax-2024.pdf", self.sent(sdk))

    async def test_a_long_unmarked_result_is_cut_too(self):
        self.look = {"names": "x" * 20000}
        sdk = self.sdk(calls(tool_use("t1", "plan_look")), reply(text("ok")))
        await self.task()
        self.assertLessEqual(len(self.sent(sdk)), 4000)

    async def test_a_request_from_a_tainted_conversation_asks_first(self):
        """The voice model wrote the goal, and could have copied a file into it."""
        sdk = self.sdk(reply(text("ok")))
        session = self.session()
        session.tainted = True
        self.clicks.answers = [False]
        self.assertEqual(await self.task(self.SECRET, session),
                         {"answer": planner.NOT_SENT, "steps": 0})
        self.assertEqual(self.clicks.asked[0].title, "Send this request to Claude?")
        self.assertEqual(self.clicks.asked[0].detail, self.SECRET)
        self.assertEqual(sdk.requests, [])

    async def test_a_yes_to_that_lets_the_task_run(self):
        sdk = self.sdk(reply(text("ok")))
        session = self.session()
        session.tainted = True
        self.clicks.answers = [True]
        self.assertEqual((await self.task("find it", session))["answer"], "ok")
        self.assertEqual(sdk.requests[0]["messages"], [{"role": "user", "content": "find it"}])

    async def test_a_clean_conversation_is_not_asked(self):
        self.sdk(reply(text("ok")))
        await self.task()
        self.assertEqual(self.clicks.asked, [])

    async def test_a_tainted_conversation_at_home_is_not_asked(self):
        self.config.values["cloud_enabled"] = False
        session = self.session()
        session.tainted = True
        with mock.patch("tuxistant.realtime._request", Server(said("ok"))):
            await self.task(session=session)
        self.assertEqual(self.clicks.asked, [])


class Sdk(CloudCase):
    """Loading the optional library, with the real import system."""

    def setUp(self):
        super().setUp()
        if importlib.util.find_spec("anthropic"):
            self.skipTest("this Python has the real SDK, so it could not go missing")
        self.addCleanup(sys.modules.pop, "anthropic", None)

    async def test_a_missing_sdk_says_how_to_get_it(self):
        result = await self.task()
        self.assertEqual(result, {"answer": planner.NO_SDK, "steps": 1})
        self.assertIn("installer", result["answer"])

    async def test_its_folder_joins_the_path_only_when_it_is_needed(self):
        self.assertNotIn(str(planner.VENDOR), sys.path)
        with mock.patch("tuxistant.realtime._request", Server(said("ok"))):
            self.config.values["cloud_enabled"] = False
            await self.task()
        self.assertNotIn(str(planner.VENDOR), sys.path)
        self.config.values["cloud_enabled"] = True
        await self.task()
        self.assertEqual(sys.path[0], str(planner.VENDOR))

    async def test_its_folder_joins_the_path_once(self):
        await self.task()
        await self.task()
        self.assertEqual(sys.path.count(str(planner.VENDOR)), 1)

    async def test_rerunning_the_installer_works_without_restarting_the_daemon(self):
        self.assertEqual((await self.task())["answer"], planner.NO_SDK)
        (planner.VENDOR / "anthropic").mkdir(parents=True)
        (planner.VENDOR / "anthropic/__init__.py").write_text(SDK_ON_DISK)
        self.assertEqual((await self.task())["answer"], "From the folder.")

    def test_importing_the_planner_loads_no_sdk(self):
        check = ("import sys, tuxistant.planner as p; "
                 "assert 'anthropic' not in sys.modules; assert str(p.VENDOR) not in sys.path")
        done = subprocess.run([sys.executable, "-P", "-c", check], capture_output=True, text=True,
                              env={**os.environ, "PYTHONPATH": str(ROOT)}, timeout=30)
        self.assertEqual(done.returncode, 0, done.stderr)


class Wiring(Case):
    def test_the_manifest(self):
        skills = Skills()
        action = skills.actions["task.run"]
        self.assertEqual(action["risk"], "high")
        self.assertIs(action["permissions"]["shell"], True)
        self.assertEqual(action["required"], ["goal"])
        self.assertEqual(list(action["params"]), ["goal"])
        self.assertIn("task_run", [tool["name"] for tool in skills.tools()])

    def test_the_handler_may_use_the_session(self):
        self.assertTrue(inspect.iscoroutinefunction(skills_module.HANDLERS["task.run"]))

    async def test_through_a_realtime_turn(self):
        """The real session: busy reaches the turn, and cards land on the task's record."""
        self.use({"plan.send": lambda *_: Ask("Send it?", "d", lambda: {"sent": True})})
        events, sent = [], []
        turn = Realtime(self.config, self.skills, events.append)
        turn.confirm = Clicks(True)

        async def send(event):
            sent.append(event)
        turn._send = send
        server = Server(asks(("plan_send", "{}")), said("Sent."))
        with mock.patch("tuxistant.realtime._request", server):
            await turn._run_tool("task_run", '{"goal": "send it"}', "call1")
        self.assertEqual(json.loads(sent[0]["item"]["output"]), {"answer": "Sent.", "steps": 2})
        self.assertIn(("busy", planner.LOCAL_WAIT_S), events)
        records = [event[1] for event in events if event[0] == "action"]
        self.assertEqual([record["name"] for record in records], ["plan.send", "task.run"])
        self.assertEqual([(a["title"], a["allowed"]) for a in records[0]["asked"]],
                         [("Send it?", True)])

    async def test_a_cancel_gets_through(self):
        started = threading.Event()
        release = threading.Event()
        self.addCleanup(release.set)

        def stuck(*_):
            started.set()
            release.wait(5)
            return json.dumps(said("too late")).encode()
        with mock.patch("tuxistant.realtime._request", stuck):
            task = asyncio.create_task(self.task())
            await asyncio.to_thread(started.wait, 5)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task


if __name__ == "__main__":
    unittest.main()
