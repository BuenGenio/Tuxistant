"""Code skills over MCP: real skillkit servers run as plain subprocesses."""
import asyncio
import os
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tuxistant import mcp  # noqa: E402
from tuxistant import skillkit  # noqa: E402

SERVER = """
import os, sys, time
from skillkit import Skill
skill = Skill("org.example.test")

@skill.action("t.echo", "Echo the arguments.", {"text": {"type": "string"}})
def echo(args, config):
    print("handlers may print; it must not break the protocol")
    return {"echo": args, "pid": os.getpid()}

@skill.action("t.boom")
def boom(args, config):
    raise RuntimeError("The lamp is on fire.")

@skill.action("t.config")
def show_config(args, config):
    return {"config": config}

@skill.action("t.sleep")
def sleep(args, config):
    time.sleep(args.get("s", 5))
    return {"slept": args.get("s", 5)}

@skill.action("t.crash")
def crash(args, config):
    os._exit(3)

@skill.action("t.big")
def big(args, config):
    return {"blob": "x" * args["n"]}

@skill.action("t.garbage")
def garbage(args, config):
    os.write(1, b"hello there\\n")
    time.sleep(5)
    return {}

@skill.action("t.list")
def not_an_object(args, config):
    return [1, 2]

@skill.action("t.escape")
def escape(args, config):
    import subprocess
    child = subprocess.Popen(["sleep", "30"], start_new_session=True)
    open(args["pidfile"], "w").write(str(child.pid))
    os._exit(3)

@skill.action("t.shout")
def shout(args, config):
    sys.stderr.write("e" * 100_000)
    sys.stderr.flush()
    os._exit(1)

skill.run()
"""


class Base(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        home = Path(self.tmp.name)
        self.env = {"PATH": "/usr/bin:/bin", "HOME": str(home),
                    "XDG_DATA_HOME": str(home / "data"),
                    "XDG_CONFIG_HOME": str(home / "config"),
                    "XDG_STATE_HOME": str(home / "state"),
                    "PYTHONPATH": str(ROOT / "tuxistant"),
                    "PYTHONDONTWRITEBYTECODE": "1", "LANG": "C.UTF-8"}
        self.processes = []

    async def asyncTearDown(self):
        for process in self.processes:
            await process.stop(graceful=False)

    def server(self, source=SERVER, **kwargs) -> mcp.SkillProcess:
        script = Path(self.tmp.name) / f"server{len(self.processes)}.py"
        script.write_text(textwrap.dedent(source))
        launch = lambda: mcp.Launch(argv=[sys.executable, str(script)],
                                    cwd=self.tmp.name, env=self.env)
        process = mcp.SkillProcess("Test skill", launch, kwargs.pop("config", {}),
                                   **kwargs)
        self.processes.append(process)
        return process


class Talking(Base):
    async def test_list_tools(self):
        tools = {t["name"]: t for t in await self.server().list_tools()}
        self.assertIn("t.echo", tools)
        self.assertEqual(tools["t.echo"]["description"], "Echo the arguments.")
        self.assertEqual(tools["t.echo"]["inputSchema"]["properties"],
                         {"text": {"type": "string"}})

    async def test_call_returns_the_object(self):
        result = await self.server().call("t.echo", {"text": "hi"})
        self.assertEqual(result["echo"], {"text": "hi"})

    async def test_calls_reuse_one_process(self):
        process = self.server()
        first = await process.call("t.echo", {})
        second = await process.call("t.echo", {})
        self.assertEqual(first["pid"], second["pid"])

    async def test_handler_exception_is_an_error(self):
        result = await self.server().call("t.boom", {})
        self.assertEqual(result, {"error": "The lamp is on fire."})

    async def test_unknown_tool_is_an_error(self):
        result = await self.server().call("t.nope", {})
        self.assertIn("no action called t.nope", result["error"])

    async def test_result_must_be_an_object(self):
        result = await self.server().call("t.list", {})
        self.assertIn("error", result)

    async def test_config_reaches_handlers_and_can_change(self):
        process = self.server(config={"tapo_email": "a@example.com"})
        result = await process.call("t.config", {})
        self.assertEqual(result, {"config": {"tapo_email": "a@example.com"}})
        await process.set_config({"tapo_email": "b@example.com"})
        result = await process.call("t.config", {})
        self.assertEqual(result, {"config": {"tapo_email": "b@example.com"}})

    async def test_set_config_before_start_is_used_at_start(self):
        process = self.server(config={"tapo_email": "a"})
        await process.set_config({"tapo_email": "c"})
        self.assertFalse(process.running)
        self.assertEqual(await process.call("t.config", {}),
                         {"config": {"tapo_email": "c"}})

    async def test_concurrent_calls_get_their_own_answers(self):
        process = self.server()
        results = await asyncio.gather(
            *(process.call("t.echo", {"n": n}) for n in range(5)))
        self.assertEqual([r["echo"]["n"] for r in results], list(range(5)))


class Misbehaving(Base):
    async def test_timeout_kills_and_the_next_call_restarts(self):
        process = self.server()
        pid = (await process.call("t.echo", {}))["pid"]
        started = time.monotonic()
        result = await process.call("t.sleep", {"s": 30}, timeout=0.5)
        self.assertLess(time.monotonic() - started, 5)
        self.assertIn("took too long", result["error"])
        await self.gone(pid)
        again = await process.call("t.echo", {})
        self.assertNotEqual(again["pid"], pid)

    async def test_crash_restarts_lazily_with_backoff(self):
        process = self.server()
        pid = (await process.call("t.echo", {}))["pid"]
        self.assertEqual(await process.call("t.crash", {}),
                         {"error": "Test skill stopped unexpectedly."})
        await self.gone(pid)
        self.assertFalse(process.running)
        second = (await process.call("t.echo", {}))["pid"]
        self.assertNotEqual(second, pid)
        # Two crashes in a row with no good answer between: it rests a moment.
        await process.call("t.crash", {})
        await process.call("t.crash", {})
        resting = await process.call("t.echo", {})
        self.assertIn("try again shortly", resting["error"])
        self.assertFalse(process.running)

    async def test_good_answer_resets_the_backoff(self):
        process = self.server()
        for _ in range(3):
            await process.call("t.crash", {})
            self.assertIn("echo", await process.call("t.echo", {}))

    async def test_oversized_line_kills(self):
        process = self.server(max_message=10_000)
        pid = (await process.call("t.echo", {}))["pid"]
        self.assertEqual(len((await process.call("t.big", {"n": 100}))["blob"]), 100)
        result = await process.call("t.big", {"n": 50_000})
        self.assertIn("too big", result["error"])
        await self.gone(pid)

    async def test_non_json_output_kills(self):
        process = self.server()
        pid = (await process.call("t.echo", {}))["pid"]
        started = time.monotonic()
        result = await process.call("t.garbage", {})
        self.assertLess(time.monotonic() - started, 4)
        self.assertIn("isn't a message", result["error"])
        await self.gone(pid)

    async def test_crash_is_noticed_when_a_child_holds_the_pipes(self):
        process = self.server()
        pidfile = Path(self.tmp.name) / "child.pid"
        started = time.monotonic()
        result = await process.call("t.escape", {"pidfile": str(pidfile)})
        try:
            self.assertIn("stopped unexpectedly", result["error"])
            self.assertLess(time.monotonic() - started, 8)
            self.assertIn("echo", await process.call("t.echo", {}))
        finally:
            if pidfile.exists():
                try:
                    os.kill(int(pidfile.read_text()), 9)
                except ProcessLookupError:
                    pass

    async def test_stderr_is_capped_in_the_problem(self):
        process = self.server()
        result = await process.call("t.shout", {})
        self.assertIn("stopped unexpectedly", result["error"])
        self.assertIn("eeee", process.problem)
        self.assertLess(len(process.problem), 4096 + 200)

    async def test_a_server_that_wont_start(self):
        process = self.server("import sys\nsys.stderr.write('no module named x')\n")
        result = await process.call("t.echo", {})
        self.assertEqual(result, {"error": "Test skill could not start."})
        self.assertIn("no module named x", process.problem)
        self.assertEqual(await process.list_tools(), [])

    async def test_a_missing_program(self):
        launch = lambda: mcp.Launch(argv=[str(Path(self.tmp.name) / "nope")])
        process = mcp.SkillProcess("Test skill", launch, {})
        self.assertEqual(await process.call("t.echo", {}),
                         {"error": "Test skill could not start."})

    async def test_launch_failure_is_an_error(self):
        def launch():
            raise ValueError("The skill folder is gone.")
        process = mcp.SkillProcess("Test skill", launch, {})
        self.assertEqual(await process.call("t.echo", {}),
                         {"error": "Test skill could not start."})
        self.assertIn("folder is gone", process.problem)

    async def test_raw_peer_shapes(self):
        # A hand-written peer: a bad reply shape, then a JSON-RPC error.
        source = r'''
        import json, sys
        for line in sys.stdin:
            m = json.loads(line)
            if "id" not in m:
                continue
            if m["method"] == "initialize":
                r = {"result": {"protocolVersion": "2025-06-18"}}
            elif m["method"] == "tools/list":
                r = {"result": {"tools": [{"name": "a"}, "junk", {"nope": 1}]}}
            elif m["params"]["name"] == "shape":
                r = {"result": {"content": "not a list"}}
            elif m["params"]["name"] == "text":
                r = {"result": {"content": [{"type": "text", "text": "plain words"}]}}
            else:
                r = {"error": {"code": -1, "message": "No lamp here"}}
            r.update(jsonrpc="2.0", id=m["id"])
            print(json.dumps(r), flush=True)
        '''
        process = self.server(source)
        self.assertEqual([t["name"] for t in await process.list_tools()], ["a"])
        self.assertIn("couldn't read", (await process.call("shape", {}))["error"])
        self.assertIn("couldn't read", (await process.call("text", {}))["error"])
        self.assertEqual(await process.call("other", {}),
                         {"error": "Test skill could not do that: No lamp here."})

    async def gone(self, pid):
        for _ in range(50):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            await asyncio.sleep(0.05)
        self.fail(f"process {pid} is still running")


class Stopping(Base):
    async def test_idle_stop_then_lazy_restart(self):
        process = self.server(idle_s=0.3)
        pid = (await process.call("t.echo", {}))["pid"]
        await asyncio.sleep(1.0)
        self.assertFalse(process.running)
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)
        again = await process.call("t.echo", {})
        self.assertNotEqual(again["pid"], pid)

    async def test_idle_stop_is_not_a_crash(self):
        process = self.server(idle_s=0.1)
        for _ in range(3):
            self.assertIn("echo", await process.call("t.echo", {}))
            await asyncio.sleep(0.5)
        self.assertIsNone(process.problem)

    async def test_busy_process_is_not_idle_stopped(self):
        process = self.server(idle_s=0.2)
        result = await process.call("t.sleep", {"s": 0.8})
        self.assertEqual(result, {"slept": 0.8})

    async def test_graceful_stop_waits_for_the_call(self):
        process = self.server()
        await process.list_tools()
        call = asyncio.create_task(process.call("t.sleep", {"s": 0.5}))
        await asyncio.sleep(0.1)
        await process.stop(graceful=True)
        self.assertEqual(await call, {"slept": 0.5})
        self.assertFalse(process.running)
        late = await process.call("t.echo", {})
        self.assertIn("switched off", late["error"])

    async def test_hard_stop_ends_the_call(self):
        process = self.server()
        await process.list_tools()
        call = asyncio.create_task(process.call("t.sleep", {"s": 30}))
        await asyncio.sleep(0.2)
        started = time.monotonic()
        await process.stop(graceful=False)
        self.assertIn("error", await call)
        self.assertLess(time.monotonic() - started, 3)

    async def test_stop_before_start_is_fine(self):
        await self.server().stop()


class Kit(unittest.TestCase):
    """The server side, without a process."""

    def setUp(self):
        self.skill = skillkit.Skill("org.example.kit")
        self.skill.action("k.add")(lambda a, c: {"sum": a["x"] + a["y"]})

    def ask(self, method, params=None, mid=1):
        import json
        line = json.dumps({"jsonrpc": "2.0", "id": mid, "method": method,
                           "params": params or {}})
        return self.skill.handle(line.encode())

    def test_initialize(self):
        result = self.ask("initialize", {"protocolVersion": "2025-06-18"})["result"]
        self.assertEqual(result["protocolVersion"], "2025-06-18")
        self.assertIn("tools", result["capabilities"])

    def test_garbage_lines_get_errors(self):
        self.assertEqual(self.skill.handle(b"nope")["error"]["code"], -32700)
        self.assertEqual(self.skill.handle(b"[1]")["error"]["code"], -32600)
        self.assertEqual(self.ask("what")["error"]["code"], -32601)

    def test_bad_arguments_are_an_error_result(self):
        result = self.ask("tools/call", {"name": "k.add", "arguments": {"x": 1}})
        self.assertTrue(result["result"]["isError"])
        self.assertEqual(result["result"]["content"][0]["text"], "'y'")

    def test_config_notification_is_silent(self):
        import json
        line = json.dumps({"jsonrpc": "2.0", "method": "tuxistant/config",
                           "params": {"values": {"k": "v"}}})
        self.assertIsNone(self.skill.handle(line.encode()))
        self.assertEqual(self.skill.config, {"k": "v"})

    def test_skillkit_imports_nothing_from_tuxistant(self):
        import ast
        tree = ast.parse((ROOT / "tuxistant" / "skillkit.py").read_text())
        names = {alias.name for node in ast.walk(tree)
                 if isinstance(node, ast.Import) for alias in node.names}
        names |= {node.module or "." for node in ast.walk(tree)
                  if isinstance(node, ast.ImportFrom)}
        self.assertLessEqual(names, {"json", "sys"})


if __name__ == "__main__":
    unittest.main()
