"""knowledge.answer: the same model, asked again with no tools in front of it."""
import asyncio
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tuxistant import knowledge, realtime  # noqa: E402
from tuxistant.config import DEFAULTS  # noqa: E402
from tuxistant.gate import Session  # noqa: E402
from tuxistant.skills import Skills  # noqa: E402


def reply(text):
    return json.dumps({"choices": [{"message": {"content": text}}]}).encode()


class Answer(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.sent = []

        def request(_config, path, body=None):
            self.sent.append((path, body))
            return reply("There are 118.")

        patcher = mock.patch.object(realtime, "_request", request)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def ask(self, question):
        name, result = await Skills(config=DEFAULTS).execute(
            "knowledge_answer", json.dumps({"question": question}), Session())
        self.assertEqual(name, "knowledge.answer")
        return result

    async def test_the_model_answers(self):
        self.assertEqual((await self.ask("How many elements are there?"))["answer"], "There are 118.")

    async def test_it_is_asked_with_no_tools_in_front_of_it(self):
        await self.ask("How many elements are there?")
        ((path, body),) = self.sent
        self.assertEqual(path, "/v1/chat/completions")
        self.assertNotIn("tools", body)
        self.assertIn("How many elements are there?", body["messages"][-1]["content"])

    async def test_no_question_goes_nowhere(self):
        self.assertEqual((await self.ask("  "))["error"], knowledge.NOT_CLEAR)
        self.assertEqual(self.sent, [])

    async def test_a_question_stays_on_your_own_server_so_needs_no_card(self):
        session = Session(Skills(config=DEFAULTS), None)
        session.tainted = True
        _, result = await session.skills.execute(
            "knowledge_answer", json.dumps({"question": "Who wrote it?"}), session)
        self.assertEqual(result["answer"], "There are 118.")

    async def test_a_slow_model_is_a_spoken_failure_not_a_hang(self):
        def stuck(*_args):
            raise TimeoutError

        with mock.patch.object(realtime, "_request", stuck):
            result = await asyncio.wait_for(self.ask("Who wrote it?"), 5)
        self.assertIn("error", result)


if __name__ == "__main__":
    unittest.main()
