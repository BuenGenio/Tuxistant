"""Write a Tuxistant code skill in a few lines.

    from skillkit import Skill
    skill = Skill("org.example.x")

    @skill.action("x.do")
    def do(args: dict, config: dict) -> dict:
        return {"done": True}

    skill.run()

The daemon starts the skill as its own process and talks MCP over stdin and
stdout: one JSON-RPC 2.0 message per line. This file is bind-mounted alone
into the sandbox, so it uses the standard library only and imports nothing
from tuxistant.
"""
import json
import sys

PROTOCOL = "2025-06-18"


class Skill:
    def __init__(self, skill_id: str, version: str = "1.0.0"):
        self.id = skill_id
        self.version = version
        self.config: dict = {}
        self._actions: dict = {}

    def action(self, name: str, description: str = "",
               params: dict | None = None, required: list | None = None):
        """Register `fn(args, config) -> dict` as the tool `name`."""
        def register(fn):
            self._actions[name] = (fn, {
                "name": name, "description": description or fn.__doc__ or "",
                "inputSchema": {"type": "object", "properties": params or {},
                                "required": list(required or [])}})
            return fn
        return register

    def run(self) -> None:
        """Serve until stdin closes."""
        out = sys.stdout.buffer
        # A stray print() in a handler would corrupt the protocol stream.
        sys.stdout = sys.stderr
        for line in sys.stdin.buffer:
            if not line.strip():
                continue
            reply = self.handle(line)
            if reply is not None:
                out.write(json.dumps(reply, ensure_ascii=False).encode() + b"\n")
                out.flush()

    def handle(self, line: bytes) -> dict | None:
        """One incoming line -> the reply to send, or None for a notification."""
        try:
            message = json.loads(line)
        except ValueError:
            return _error(None, -32700, "That was not JSON.")
        if not isinstance(message, dict):
            return _error(None, -32600, "A message must be an object.")
        method, params = message.get("method"), message.get("params") or {}
        if "id" not in message:
            self._notified(method, params)
            return None
        handler = {"initialize": self._initialize, "ping": lambda p: {},
                   "tools/list": self._list, "tools/call": self._call}.get(method)
        if handler is None:
            return _error(message["id"], -32601, f"Unknown method {method}.")
        return {"jsonrpc": "2.0", "id": message["id"], "result": handler(params)}

    def _notified(self, method, params) -> None:
        if method == "tuxistant/config" and isinstance(params, dict):
            values = params.get("values")
            self.config = dict(values) if isinstance(values, dict) else {}

    def _initialize(self, params) -> dict:
        return {"protocolVersion": PROTOCOL, "capabilities": {"tools": {}},
                "serverInfo": {"name": self.id, "version": self.version}}

    def _list(self, params) -> dict:
        return {"tools": [schema for _, schema in self._actions.values()]}

    def _call(self, params) -> dict:
        name = params.get("name") if isinstance(params, dict) else None
        if name not in self._actions:
            return _result(f"There is no action called {name}.", True)
        args = params.get("arguments")
        fn = self._actions[name][0]
        try:
            value = fn(args if isinstance(args, dict) else {}, dict(self.config))
        except Exception as e:  # the handler's own words are the best error text
            return _result(str(e) or type(e).__name__, True)
        if not isinstance(value, dict):
            return _result(f"{name} returned something that isn't an object.", True)
        return _result(json.dumps(value, ensure_ascii=False), False)


def _result(text: str, is_error: bool) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def _error(message_id, code: int, text: str) -> dict:
    return {"jsonrpc": "2.0", "id": message_id,
            "error": {"code": code, "message": text}}
