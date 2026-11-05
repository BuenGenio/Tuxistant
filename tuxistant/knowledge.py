"""Plain facts. The small model knows them, but with its tools in front of it it refuses or guesses
about one time in five. Asked on its own, with no tools, it answered all of them."""
from .research import _brain

ANSWER_TOKENS = 80
TIMEOUT_S = 20
NOT_CLEAR = "I didn't catch the question."
ASK = "Answer the question in one short plain sentence. /no_think"


async def answer(args: dict, config, session) -> dict:
    question = str(args.get("question", "")).strip()
    if not question:
        return {"error": NOT_CLEAR}
    return {"answer": await _brain(config, ASK, question, ANSWER_TOKENS, TIMEOUT_S)}


HANDLERS = {"knowledge.answer": answer}
