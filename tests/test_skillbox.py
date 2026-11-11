"""The code-skill sandbox, with real bubblewrap where it is installed.

No test here sends a packet: without the network permission the skill has no interface but a
loopback that is down, and with it the tests only connect to listeners they made themselves on
this computer's loopback, which the sandbox must refuse."""
import os
import subprocess
import tempfile
import textwrap
import unittest
from dataclasses import dataclass
from pathlib import Path
from unittest import mock

_TEMP = tempfile.TemporaryDirectory(prefix="tux-skillbox-")
for _name in ("XDG_DATA_HOME", "XDG_CONFIG_HOME", "XDG_STATE_HOME"):
    os.environ.setdefault(_name, os.path.join(_TEMP.name, _name.lower()))

from tuxistant import shellgate, skillbox  # noqa: E402

REAL_HOME = os.path.realpath(os.path.expanduser("~"))
RUN_S = 30


@dataclass
class StandInLaunch:
    """Used only while tuxistant/mcp.py has no Launch yet: the same fields, and close()
    closes what was passed."""
    argv: list
    pass_fds: tuple = ()
    cwd: str | None = None
    env: dict | None = None

    def close(self) -> None:
        for fd in self.pass_fds:
            try:
                os.close(fd)
            except OSError:
                pass


_patch = None


def setUpModule():
    global _patch
    try:
        from tuxistant.mcp import Launch  # noqa: F401
    except ImportError:
        _patch = mock.patch.object(skillbox, "_launch_class", lambda: StandInLaunch)
        _patch.start()


def tearDownModule():
    if _patch is not None:
        _patch.stop()


def needs_box(test):
    return unittest.skipUnless(skillbox.available(), "bubblewrap or the skill kit is missing")(test)


class Folders(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="tux-skillbox-")
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.folder, self.state = root / "skill", root / "state" / "org.example.box"
        self.folder.mkdir()

    def write(self, name: str, text: str, mode: int = 0o644) -> None:
        path = self.folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(text))
        path.chmod(mode)

    def prepare(self, run, permissions=None):
        return skillbox.prepare(self.folder, run, permissions or {}, self.state)

    def run_box(self, script: str, permissions=None, extra_env=None):
        self.write("main.py", script)
        launch = self.prepare(["python3", "main.py"], permissions)
        return spawn(launch, extra_env)


def spawn(launch, extra_env=None) -> subprocess.CompletedProcess:
    """As SkillProcess does it: spawn with the Launch, then close what it passed."""
    env = None if launch.env is None else {**launch.env, **(extra_env or {})}
    try:
        return subprocess.run(launch.argv, pass_fds=launch.pass_fds, cwd=launch.cwd, env=env,
                              stdin=subprocess.DEVNULL, capture_output=True, text=True,
                              timeout=RUN_S)
    finally:
        launch.close()


class RunLine(Folders):
    def setUp(self):
        super().setUp()
        self.write("main.py", "print('hi')\n")
        self.write("tool.sh", "#!/bin/sh\necho hi\n", 0o755)

    def refused(self, run, permissions=None):
        with self.assertRaises(ValueError) as caught:
            skillbox.command(self.folder, run) if permissions is None else \
                self.prepare(run, permissions)
        return str(caught.exception)

    def test_python_maps_to_isolated_system_python(self):
        argv = skillbox.command(self.folder.resolve(), ["python3", "main.py", "--fast"])
        self.assertEqual(argv[:3], ["/usr/bin/python3", "-I", "-B"])
        self.assertEqual(argv[-2:], ["/skill/main.py", "--fast"])

    def test_node_maps_to_system_node(self):
        argv = skillbox.command(self.folder.resolve(), ["node", "main.py"])
        self.assertEqual(argv, ["/usr/bin/node", "--", "/skill/main.py"])

    def test_own_program_runs_from_the_skill_folder(self):
        self.assertEqual(skillbox.command(self.folder.resolve(), ["tool.sh", "x"]),
                         ["/skill/tool.sh", "x"])

    def test_dot_dot_is_refused(self):
        for run in (["python3", "../main.py"], ["python3", "sub/../../x.py"],
                    ["python3", "main.py", "../secret"], ["../tool.sh"]):
            with self.subTest(run=run):
                self.assertEqual(self.refused(run), skillbox.BAD_PATH)

    def test_absolute_paths_are_refused(self):
        for run in (["/usr/bin/python3", "main.py"], ["python3", "/etc/passwd"],
                    ["python3", "main.py", "/home"], [str(self.folder / "tool.sh")]):
            with self.subTest(run=run):
                self.assertEqual(self.refused(run), skillbox.BAD_PATH)

    def test_unknown_interpreter_is_refused(self):
        for run in (["bash", "main.py"], ["ruby", "main.py"], ["sh", "-c", "id"]):
            with self.subTest(run=run):
                self.assertEqual(self.refused(run), skillbox.NO_SCRIPT)

    def test_missing_or_empty_run_is_refused(self):
        for run in ([], None, "python3 main.py", ["python3"], [""], ["python3", "gone.py"]):
            with self.subTest(run=run):
                self.refused(run)

    def test_a_link_out_of_the_folder_is_refused(self):
        (self.folder / "out.py").symlink_to("/etc/hostname")
        self.assertEqual(self.refused(["python3", "out.py"]), skillbox.NO_SCRIPT)

    def test_a_null_byte_is_refused(self):
        self.assertEqual(self.refused(["python3", "main.py", "a\0b"]), skillbox.BAD_PATH)

    def test_only_network_may_be_asked_for(self):
        for permissions in ({"shell": True}, {"files_read": ["~"]}, {"files_write": True},
                            {"clipboard": True}, {"network": True, "shell": True}, ["network"]):
            with self.subTest(permissions=permissions):
                self.assertEqual(self.refused(["python3", "main.py"], permissions),
                                 skillbox.BAD_PERMISSION)

    def test_state_folder_names(self):
        with mock.patch.object(skillbox, "STATE_ROOT", Path(self.temp.name) / "states"):
            self.assertEqual(skillbox.state_dir("org.example.x"),
                             Path(self.temp.name) / "states" / "org.example.x")
        for bad in ("", ".", "..", "a/b", "../x", "a\0"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                skillbox.state_dir(bad)


@needs_box
class LaunchShape(Folders):
    def setUp(self):
        super().setUp()
        self.write("main.py", "print('hi')\n")

    def test_filter_descriptor_is_passed_and_closed(self):
        launch = self.prepare(["python3", "main.py"])
        self.assertEqual(len(launch.pass_fds), 1)
        fd = launch.pass_fds[0]
        at = launch.argv.index("--seccomp")
        self.assertEqual(launch.argv[at + 1], str(fd))
        self.assertEqual(os.read(fd, 1 << 16), shellgate.seccomp_program())
        launch.close()
        with self.assertRaises(OSError):
            os.fstat(fd)

    def test_memory_is_capped_as_for_the_shell(self):
        launch = self.prepare(["python3", "main.py"])
        self.addCleanup(launch.close)
        self.assertEqual(launch.argv[:3], [shellgate.PRLIMIT,
                                           f"--as={shellgate.SANDBOX_MEMORY_BYTES}",
                                           shellgate.BWRAP])

    def test_network_flag_follows_the_permission(self):
        for permissions, shared in (({}, False), ({"network": False}, False),
                                    ({"network": True}, True), ({"network": ["a.example"]}, True)):
            with self.subTest(permissions=permissions):
                launch = self.prepare(["python3", "main.py"], permissions)
                launch.close()
                self.assertEqual("--share-net" in launch.argv, shared)
                self.assertEqual("/etc/resolv.conf" in launch.argv, shared)

    def test_state_folder_is_private(self):
        self.prepare(["python3", "main.py"]).close()
        self.assertEqual(self.state.stat().st_mode & 0o777, 0o700)

    def test_nothing_is_left_open_when_refused(self):
        before = set(os.listdir("/proc/self/fd"))
        with self.assertRaises(ValueError):
            self.prepare(["python3", "main.py"], {"shell": True})
        self.assertEqual(set(os.listdir("/proc/self/fd")), before)


@needs_box
class Sandboxed(Folders):
    def check(self, script: str, permissions=None, extra_env=None) -> str:
        done = self.run_box(script, permissions, extra_env)
        self.assertEqual(done.returncode, 0, done.stderr)
        return done.stdout

    def test_no_network_without_permission(self):
        out = self.check("""
            import errno, socket
            print(sorted(name for _, name in socket.if_nameindex()))
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                s.connect(("1.1.1.1", 53))
                print("connected")
            except OSError as e:
                print("unreachable", e.errno == errno.ENETUNREACH)
            try:
                socket.getaddrinfo("example.com", 443)
                print("resolved")
            except OSError:
                print("no names")
        """)
        self.assertEqual(out.split("\n")[:3], ["['lo']", "unreachable True", "no names"])

    def test_network_permission_allows_internet_sockets(self):
        out = self.check("""
            import errno, os, socket
            for family in (socket.AF_INET, socket.AF_INET6, socket.AF_UNIX):
                socket.socket(family, socket.SOCK_STREAM).close()
            print("sockets ok", os.path.exists("/etc/resolv.conf"))
            try:
                socket.socket(socket.AF_VSOCK, socket.SOCK_STREAM)
                print("vsock")
            except OSError as e:
                print("no vsock", e.errno == errno.EAFNOSUPPORT)
        """, {"network": True})
        self.assertEqual(out.split("\n")[:2], ["sockets ok True", "no vsock True"])

    def test_seccomp_filter_is_loaded(self):
        out = self.check("""
            import errno, socket
            try:
                socket.socket(socket.AF_VSOCK, socket.SOCK_STREAM)
                print("vsock")
            except OSError as e:
                print(e.errno == errno.EAFNOSUPPORT)
        """)
        self.assertEqual(out.strip(), "True")

    def test_home_folder_is_not_there(self):
        out = self.check(f"""
            import os
            home = {REAL_HOME!r}
            for probe in (lambda: os.listdir(home), lambda: open(home + "/.bashrc").read(),
                          lambda: os.listdir("/home")):
                try:
                    probe()
                    print("seen")
                except OSError:
                    print("hidden")
            print(os.environ["HOME"], os.path.expanduser("~"))
        """)
        self.assertEqual(out.split("\n")[:4], ["hidden", "hidden", "hidden", "/state /state"])

    def test_skill_folder_is_read_only(self):
        out = self.check("""
            import errno, os
            print(os.getcwd(), sorted(os.listdir("/skill")))
            for path in ("/skill/new.txt", "/skill/main.py", "/etc/new", "/new"):
                try:
                    open(path, "a").close()
                    print("written")
                except OSError as e:
                    print(e.errno in (errno.EROFS, errno.EACCES))
        """)
        self.assertEqual(out.split("\n")[:5], ["/skill ['main.py']", "True", "True", "True", "True"])
        self.assertFalse((self.folder / "new.txt").exists())

    def test_state_folder_is_writable(self):
        out = self.check("""
            import os
            with open("/state/remember.txt", "w") as f:
                f.write("kept")
            os.makedirs("/state/sub", exist_ok=True)
            open("/tmp/scratch", "w").write("x")
            print("ok")
        """)
        self.assertEqual(out.strip(), "ok")
        self.assertEqual((self.state / "remember.txt").read_text(), "kept")
        self.assertTrue((self.state / "sub").is_dir())

    def test_skill_kit_is_there_and_imports(self):
        out = self.check("""
            import skillkit
            print(skillkit.__file__)
            print(open("/run/tuxistant/skillkit.py").read() == open(skillkit.__file__).read())
        """)
        self.assertEqual(out.split("\n")[:2], ["/run/tuxistant/skillkit.py", "True"])
        inside = self.check("print(open('/run/tuxistant/skillkit.py').read(), end='')")
        self.assertEqual(inside, skillbox.SKILLKIT.read_text())

    def test_own_modules_import_from_the_skill_folder(self):
        self.write("helper.py", "VALUE = 42\n")
        self.write("sub/inner.py", "import helper\nprint(helper.VALUE)\n")
        self.write("main.py", "")
        done = spawn(self.prepare(["python3", "sub/inner.py"]))
        self.assertEqual((done.returncode, done.stdout.strip()), (0, "42"), done.stderr)

    def test_environment_is_clean(self):
        leaked = {"DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus",
                  "XDG_RUNTIME_DIR": "/run/user/1000", "WAYLAND_DISPLAY": "wayland-0",
                  "DISPLAY": ":0", "TUX_SECRET": "leak"}
        out = self.check("""
            import os
            print(sorted(os.environ))
            print(os.environ["PATH"], os.environ["LANG"], os.environ["PYTHONPATH"])
            print(os.path.exists("/run/user"), os.path.exists("/tmp/.X11-unix"))
        """, extra_env=leaked)
        names, values, paths = out.split("\n")[:3]
        for name in leaked:
            self.assertNotIn(name, names)
        self.assertEqual(values, "/usr/bin:/bin C.UTF-8 /run/tuxistant")
        self.assertEqual(paths, "False False")

    def test_arguments_reach_the_skill(self):
        out = self.check("import sys\nprint(sys.argv)\n")
        self.assertEqual(out.strip(), "['/skill/main.py']")

    def test_own_program_runs(self):
        self.write("tool.sh", "#!/bin/sh\necho \"$HOME $1\"\n", 0o755)
        done = spawn(self.prepare(["tool.sh", "there"]))
        self.assertEqual((done.returncode, done.stdout.strip()), (0, "/state there"), done.stderr)

    @unittest.skipUnless(os.access(skillbox.NODE, os.X_OK), "node is not installed")
    def test_node_runs_under_the_memory_cap(self):
        self.write("main.js", "console.log(process.env.HOME, process.cwd())\n")
        done = spawn(self.prepare(["node", "main.js"]))
        self.assertEqual((done.returncode, done.stdout.strip()), (0, "/state /skill"), done.stderr)


class HostOnly(Folders):
    """The network permission shares this computer's network namespace. Services meant only
    for this computer listen there too: adb on 127.0.0.1:5037, CUPS, and the display's abstract
    sockets. Stand-ins for them here are host listeners the test makes itself; nothing leaves
    loopback, and no real service is touched."""

    def setUp(self):
        super().setUp()
        if not skillbox.available():
            self.skipTest("bubblewrap or the skill kit is missing")
        if skillbox.landlock_abi() < skillbox.LANDLOCK_SCOPE_ABI:
            self.skipTest("this kernel's Landlock can't shut out abstract sockets")
        import socket
        self.name = f"tux-skillbox-test-{os.getpid()}"
        self.abstract = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.abstract.bind("\0" + self.name)
        self.abstract.listen()
        self.tcp = socket.socket()
        self.tcp.bind(("127.0.0.1", 0))
        self.tcp.listen()
        self.addCleanup(self.abstract.close)
        self.addCleanup(self.tcp.close)
        self.port = self.tcp.getsockname()[1]

    PROBE = """
        import errno, json, socket
        def attempt(family, address):
            s = socket.socket(family, socket.SOCK_STREAM)
            s.settimeout(5)
            try:
                s.connect(address)
                return "connected"
            except OSError as e:
                return errno.errorcode.get(e.errno, str(e.errno))
            finally:
                s.close()
        def listen():
            s = socket.socket()
            try:
                s.bind(("127.0.0.1", 0))
                s.listen()
                return "listening"
            except OSError as e:
                return errno.errorcode.get(e.errno, str(e.errno))
        print(json.dumps({"abstract": attempt(socket.AF_UNIX, "\\0%s"),
                          "loopback": attempt(socket.AF_INET, ("127.0.0.1", %d)),
                          "listen": listen()}))
    """

    def probe(self, run=("python3", "main.py")) -> dict:
        import json
        self.write("main.py", self.PROBE % (self.name, self.port))
        done = spawn(self.prepare(list(run), {"network": True}))
        self.assertEqual(done.returncode, 0, done.stderr)
        return json.loads(done.stdout)

    def test_a_network_skill_cant_reach_this_computers_own_services(self):
        self.assertEqual(self.probe(), {"abstract": "EPERM", "loopback": "EACCES",
                                        "listen": "EACCES"})

    def test_the_limit_holds_for_a_skill_that_isnt_python(self):
        self.write("tool", "#!/usr/bin/python3 -I\nimport runpy\n"
                           "runpy.run_path('/skill/main.py')\n", 0o755)
        self.assertEqual(self.probe(["tool"])["loopback"], "EACCES")

    def test_an_old_kernel_gets_no_network_at_all(self):
        self.write("main.py", "print('hi')\n")
        with mock.patch.object(skillbox, "landlock_abi", return_value=5):
            with self.assertRaises(ValueError) as caught:
                self.prepare(["python3", "main.py"], {"network": True})
        self.assertEqual(str(caught.exception), skillbox.OLD_KERNEL)


@needs_box
class ThroughSkillProcess(Folders):
    """The real spawn path: mcp.SkillProcess starting a skillkit skill inside the box."""

    def test_skill_answers_from_inside_the_sandbox(self):
        try:
            from tuxistant import mcp
        except ImportError:
            self.skipTest("tuxistant/mcp.py is not there yet")
        self.write("main.py", """
            import os
            from skillkit import Skill
            skill = Skill("org.example.box")

            @skill.action("box.where")
            def where(args, config):
                return {"home": os.environ["HOME"], "cwd": os.getcwd(),
                        "email": config.get("tapo_email", "")}

            skill.run()
        """)
        launches = []

        def launch():
            launches.append(self.prepare(["python3", "main.py"]))
            return launches[-1]

        async def go():
            process = mcp.SkillProcess("Box", launch, {"tapo_email": "a@b.example"})
            try:
                tools = await process.list_tools()
                return tools, await process.call("box.where", {})
            finally:
                await process.stop()

        import asyncio
        tools, result = asyncio.run(go())
        self.assertEqual([tool["name"] for tool in tools], ["box.where"])
        self.assertEqual(result, {"home": "/state", "cwd": "/skill", "email": "a@b.example"})
        self.assertEqual(len(launches), 1)


if __name__ == "__main__":
    unittest.main()
