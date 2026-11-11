"""Run a code skill as its own process and talk MCP to it over stdio.

The peer is untrusted: every line is size-capped, anything that isn't a JSON
object kills it, a slow call kills it, and its stderr is drained and capped.
It starts on first use, restarts after a crash with a growing pause, and
stops after a while with no calls. `call` never raises; failures come back as
{"error": "<plain sentence>"} so the model can say them.
"""
import asyncio
import json
import os
import signal
import time
from dataclasses import dataclass
from typing import Callable

PROTOCOL = "2025-06-18"
START_TIMEOUT_S = 10.0
EXIT_GRACE_S = 2.0
STDERR_KEEP = 4096
TEXT_CAP = 300
# Pause before restarting after the n-th failure in a row.
BACKOFF_S = (0.0, 1.0, 2.0, 5.0, 10.0, 30.0)


@dataclass
class Launch:
    argv: list[str]
    pass_fds: tuple[int, ...] = ()
    cwd: str | None = None
    env: dict | None = None

    def close(self) -> None:
        for fd in self.pass_fds:
            try:
                os.close(fd)
            except OSError:
                pass


class _Fail(Exception):
    """A failure whose text is already a sentence fit to speak."""


def _short(text: str) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= TEXT_CAP else text[:TEXT_CAP - 3] + "..."


class _Conn:
    """One running process. Dies once; a restart makes a new one."""

    def __init__(self, name: str, proc, on_exit: Callable):
        self.name, self.proc, self._on_exit = name, proc, on_exit
        self.pending: dict[int, asyncio.Future] = {}
        self.stderr = bytearray()
        self.reason: str | None = None   # why it died, once it has
        self.intentional = False         # we stopped it, so it isn't a crash
        self.counted = False             # its failure is already in the backoff
        self._tasks: set[asyncio.Task] = set()
        self._next_id = 0
        self._drainer = asyncio.create_task(self._drain())
        self._watcher = asyncio.create_task(self._watch())
        self.reader = asyncio.create_task(self._read())

    @property
    def alive(self) -> bool:
        return self.reason is None

    @property
    def usable(self) -> bool:
        return self.alive and not self.intentional

    def kill(self, reason: str) -> None:
        if self.reason is not None:
            return
        self.reason = reason
        try:
            os.killpg(self.proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        for fut in self.pending.values():
            if not fut.done():
                fut.set_exception(_Fail(reason))
        self.pending.clear()
        # A child that escaped the kill could hold stdout open forever.
        if asyncio.current_task() is not self.reader:
            self.reader.cancel()

    async def settled(self) -> None:
        """Wait until its exit is fully handled (reaped, stderr read, reported)."""
        await asyncio.wait({self.reader})

    async def request(self, method: str, params: dict, timeout: float):
        if not self.usable:
            raise _Fail(self.reason or f"{self.name} was stopped.")
        self._next_id += 1
        mid = self._next_id
        fut = asyncio.get_running_loop().create_future()
        self.pending[mid] = fut
        try:
            async with asyncio.timeout(timeout):
                await self._send({"jsonrpc": "2.0", "id": mid,
                                  "method": method, "params": params})
                return await fut
        except TimeoutError:
            self.kill(f"{self.name} took too long to answer, so it was stopped.")
            raise _Fail(self.reason) from None
        finally:
            self.pending.pop(mid, None)

    async def notify(self, method: str, params: dict) -> None:
        if not self.usable:
            return
        try:
            async with asyncio.timeout(EXIT_GRACE_S):
                await self._send({"jsonrpc": "2.0", "method": method,
                                  "params": params})
        except (_Fail, TimeoutError):
            pass

    async def _send(self, message: dict) -> None:
        try:
            self.proc.stdin.write(json.dumps(message).encode() + b"\n")
            await self.proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError, RuntimeError):
            self.kill(f"{self.name} stopped unexpectedly.")
            raise _Fail(self.reason) from None

    async def _read(self) -> None:
        try:
            while self.alive:
                try:
                    line = await self.proc.stdout.readline()
                except (ValueError, asyncio.LimitOverrunError):
                    self.kill(f"{self.name} sent a message that was too big, "
                              "so it was stopped.")
                    break
                if not line:
                    self.kill(f"{self.name} stopped unexpectedly.")
                    break
                if line.strip():
                    self._handle(line)
        finally:
            self.kill(f"{self.name} stopped unexpectedly.")
            self._watcher.cancel()
            await self._reap()
            self._on_exit(self)

    async def _watch(self) -> None:
        # Exit is noticed by returncode, not EOF: an escaped child can keep
        # the pipes open after the skill itself is gone.
        while self.proc.returncode is None:
            await asyncio.sleep(0.25)
        await asyncio.sleep(0.5)   # let the reader take what it already sent
        self.kill(f"{self.name} stopped unexpectedly.")

    async def _reap(self) -> None:
        try:
            await asyncio.wait_for(asyncio.shield(self.proc.wait()), EXIT_GRACE_S)
        except TimeoutError:
            # wait() only returns once every pipe closes; close them ourselves.
            transport = getattr(self.proc, "_transport", None)
            if transport is not None:
                transport.close()
            await self.proc.wait()
        try:
            await asyncio.wait_for(self._drainer, EXIT_GRACE_S)
        except (TimeoutError, OSError):
            pass

    def _handle(self, line: bytes) -> None:
        try:
            message = json.loads(line)
        except ValueError:
            message = None
        if not isinstance(message, dict):
            self.kill(f"{self.name} sent something that isn't a message, "
                      "so it was stopped.")
            return
        if "method" in message:
            if "id" in message:   # the peer asking us something; we offer nothing
                task = asyncio.create_task(self._refuse(message))
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)
            return
        mid = message.get("id")
        fut = self.pending.pop(mid, None) if type(mid) is int else None
        if fut is not None and not fut.done():
            fut.set_result(message)

    async def _refuse(self, message: dict) -> None:
        reply = {"jsonrpc": "2.0", "id": message["id"]}
        if message.get("method") == "ping":
            reply["result"] = {}
        else:
            reply["error"] = {"code": -32601, "message": "Not supported."}
        try:
            await self._send(reply)
        except _Fail:
            pass

    async def _drain(self) -> None:
        while chunk := await self.proc.stderr.read(4096):
            self.stderr += chunk
            del self.stderr[:-STDERR_KEEP]

    async def close(self, grace: float = EXIT_GRACE_S) -> None:
        """Ask it to exit by closing stdin, then insist."""
        self.intentional = True
        if self.alive and grace > 0:
            try:
                self.proc.stdin.close()
            except (OSError, RuntimeError):
                pass
            try:
                await asyncio.wait_for(asyncio.shield(self.proc.wait()), grace)
            except TimeoutError:
                pass
        self.kill(f"{self.name} was stopped.")
        await self.settled()


def _result_of(name: str, message: dict) -> dict:
    """A tools/call reply -> the tool's JSON object, or an error."""
    if "error" in message:
        error = message["error"]
        text = error.get("message") if isinstance(error, dict) else None
        return {"error": _short(f"{name} could not do that: {text or 'no reason given'}.")}
    result = message.get("result")
    content = result.get("content") if isinstance(result, dict) else None
    first = content[0] if isinstance(content, list) and content else None
    text = first.get("text") if isinstance(first, dict) else None
    if not isinstance(text, str):
        return {"error": f"{name} sent an answer I couldn't read."}
    if result.get("isError"):
        return {"error": _short(text) or f"{name} could not do that."}
    try:
        value = json.loads(text)
    except ValueError:
        value = None
    if not isinstance(value, dict):
        return {"error": f"{name} sent an answer I couldn't read."}
    return value


def _tools_of(result) -> list[dict]:
    tools = result.get("tools") if isinstance(result, dict) else None
    if not isinstance(tools, list):
        return []
    return [t for t in tools if isinstance(t, dict) and isinstance(t.get("name"), str)]


class SkillProcess:
    def __init__(self, name: str, launch: Callable[[], Launch], config_values: dict,
                 call_timeout_s: float = 15, idle_s: float = 300,
                 max_message: int = 1_000_000):
        self.name = name
        self.call_timeout_s = call_timeout_s
        self.idle_s = idle_s
        self.max_message = max_message
        self._launch = launch
        self._config = dict(config_values or {})
        self._conn: _Conn | None = None
        self._tools: list[dict] = []
        self._start_lock = asyncio.Lock()
        self._busy = 0
        self._quiet = asyncio.Event()
        self._quiet.set()
        self._idle_timer: asyncio.TimerHandle | None = None
        self._closing: set[asyncio.Task] = set()
        self._failures = 0
        self._retry_at = 0.0
        self._stopped = False
        self.problem: str | None = None   # last failure, with the tail of stderr

    @property
    def running(self) -> bool:
        return self._conn is not None and self._conn.usable

    @property
    def pid(self) -> int | None:
        return self._conn.proc.pid if self.running else None

    async def list_tools(self) -> list[dict]:
        """The tools it offers, starting it if needed; [] if it won't start."""
        async with self._use():
            try:
                await self._ensure()
            except _Fail:
                return []
            return list(self._tools)

    async def call(self, tool: str, arguments: dict, timeout: float | None = None) -> dict:
        if self._stopped:
            return {"error": f"{self.name} was switched off. Please ask again."}
        conn = None
        async with self._use():
            try:
                conn = await self._ensure()
                reply = await conn.request(
                    "tools/call", {"name": tool, "arguments": dict(arguments or {})},
                    timeout or self.call_timeout_s)
            except _Fail as e:
                if conn is not None:   # so the crash is counted before the next call
                    await conn.settled()
                return {"error": str(e)}
            except Exception:  # call() must never raise into a turn
                return {"error": f"{self.name} hit an unexpected problem."}
            self._failures = 0
            return _result_of(self.name, reply)

    async def set_config(self, values: dict) -> None:
        self._config = dict(values or {})
        if self.running:
            await self._conn.notify("tuxistant/config", {"values": self._config})

    async def stop(self, graceful: bool = True) -> None:
        self._stopped = True
        self._cancel_idle()
        if graceful:
            try:
                await asyncio.wait_for(self._quiet.wait(),
                                       self.call_timeout_s + START_TIMEOUT_S)
            except TimeoutError:
                pass
        if self._conn is not None:
            await self._conn.close(EXIT_GRACE_S if graceful else 0)
        if self._closing:
            await asyncio.gather(*self._closing, return_exceptions=True)

    def _use(self):
        return _Busy(self)

    async def _ensure(self) -> _Conn:
        async with self._start_lock:
            if self._stopped:
                raise _Fail(f"{self.name} was switched off. Please ask again.")
            if self.running:
                return self._conn
            if time.monotonic() < self._retry_at:
                raise _Fail(f"{self.name} stopped working a moment ago. "
                            "Please try again shortly.")
            return await self._start()

    async def _start(self) -> _Conn:
        conn = await self._spawn()
        try:
            await self._handshake(conn)
        except _Fail:
            reason = conn.reason or f"{self.name} did not finish starting."
            await conn.close(0)
            self._failed(reason, conn)
            raise _Fail(f"{self.name} could not start.") from None
        if self._stopped:
            await conn.close()
            raise _Fail(f"{self.name} was switched off. Please ask again.")
        return conn

    async def _spawn(self) -> _Conn:
        try:
            launch = self._launch()
        except Exception as e:  # skillbox reasons are written to be spoken
            self._failed(_short(str(e) or "it could not be prepared"), None)
            raise _Fail(f"{self.name} could not start.") from None
        try:
            proc = await asyncio.create_subprocess_exec(
                *launch.argv, stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                pass_fds=launch.pass_fds, cwd=launch.cwd, env=launch.env,
                limit=self.max_message, start_new_session=True)
        except (OSError, ValueError) as e:
            self._failed(f"{self.name} could not start: {e.strerror or e}.", None)
            raise _Fail(f"{self.name} could not start.") from None
        finally:
            launch.close()
        self._conn = _Conn(self.name, proc, self._exited)
        return self._conn

    async def _handshake(self, conn: _Conn) -> None:
        reply = await conn.request("initialize", {
            "protocolVersion": PROTOCOL, "capabilities": {},
            "clientInfo": {"name": "tuxistant", "version": "1"}}, START_TIMEOUT_S)
        if not isinstance(reply.get("result"), dict):
            conn.kill(f"{self.name} refused to start.")
            raise _Fail(conn.reason)
        await conn.notify("notifications/initialized", {})
        await conn.notify("tuxistant/config", {"values": self._config})
        reply = await conn.request("tools/list", {}, START_TIMEOUT_S)
        self._tools = _tools_of(reply.get("result"))

    def _exited(self, conn: _Conn) -> None:
        if conn is self._conn:
            self._conn = None
            self._cancel_idle()
        if not conn.intentional:
            self._failed(conn.reason, conn)

    def _failed(self, reason: str, conn: _Conn | None) -> None:
        if conn is not None:
            if conn.counted:
                return
            conn.counted = True
        self._failures += 1
        pause = BACKOFF_S[min(self._failures, len(BACKOFF_S)) - 1]
        self._retry_at = time.monotonic() + pause
        tail = conn.stderr.decode(errors="replace").strip() if conn else ""
        self.problem = f"{reason} {tail[-STDERR_KEEP:]}".strip()

    def _enter(self) -> None:
        self._busy += 1
        self._quiet.clear()
        self._cancel_idle()

    def _leave(self) -> None:
        self._busy -= 1
        if self._busy == 0:
            self._quiet.set()
            if self.running and not self._stopped:
                self._idle_timer = asyncio.get_running_loop().call_later(
                    self.idle_s, self._idle)

    def _cancel_idle(self) -> None:
        if self._idle_timer is not None:
            self._idle_timer.cancel()
            self._idle_timer = None

    def _idle(self) -> None:
        self._idle_timer = None
        if self._busy == 0 and self._conn is not None:
            task = asyncio.create_task(self._conn.close())
            self._closing.add(task)
            task.add_done_callback(self._closing.discard)


class _Busy:
    """Counts a call as in flight, so idle stop and graceful stop wait for it."""

    def __init__(self, owner: SkillProcess):
        self._owner = owner

    async def __aenter__(self):
        self._owner._enter()

    async def __aexit__(self, *exc):
        self._owner._leave()
        return False
