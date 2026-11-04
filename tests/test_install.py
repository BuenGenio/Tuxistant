"""install.sh, uninstall.sh, the unit files and the docs. Everything lands in a temp prefix.

ssh, sudo and systemctl are replaced by shims on PATH that record their arguments, so neither
the dry run nor the real branch can reach this machine's services or the network.
"""
import configparser
import json
import os
import pty
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
UUID = "tuxistant@buengenio"
FAKE_KEY = "fake-key-do-not-print-0123456789"

SHIM = """#!/bin/sh
echo "$(basename "$0") $*" >> "$SHIM_LOG"
"""
SUDO_SHIM = SHIM + 'exec "$@"\n'
SSH_SHIM = SHIM + '[ -z "$SSH_REPLY" ] && exit 255\necho "$SSH_REPLY"\n'
SCHEMA = """<schemalist><schema id="org.test" path="/org/test/">
<key name="a" type="b"><default>true</default></key></schema></schemalist>"""
STUB_MAIN = "import sys, tuxistant\nprint('ran', tuxistant.__file__, sys.argv[1:])\n"


def tree(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in Path(root).rglob("*") if p.is_file()}


class Prefix(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="tuxinstall"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.project = self.tmp / "my project's copy"  # a path that needs quoting
        self.project.mkdir()
        for name in ("install.sh", "uninstall.sh"):
            shutil.copy2(ROOT / name, self.project)
        shutil.copytree(ROOT / "packaging", self.project / "packaging")
        shutil.copytree(ROOT / "helper", self.project / "helper")
        extension = self.project / "extension" / UUID
        (extension / "schemas").mkdir(parents=True)
        (extension / "metadata.json").write_text('{"uuid": "tuxistant@buengenio"}')
        (extension / "extension.js").write_text("// stub\n")
        (extension / "schemas" / "org.test.gschema.xml").write_text(SCHEMA)
        package = self.project / "tuxistant"
        package.mkdir()
        (package / "__init__.py").write_text("")
        (package / "__main__.py").write_text(STUB_MAIN)

        self.shims = self.tmp / "shims"
        self.shims.mkdir()
        for name, body in (("ssh", SSH_SHIM), ("sudo", SUDO_SHIM), ("systemctl", SHIM)):
            shim = self.shims / name
            shim.write_text(body)
            shim.chmod(0o755)
        self.shim_log = self.tmp / "shims.log"
        self.home = self.tmp / "home"
        self.data = self.tmp / "data"
        self.config = self.tmp / "config"
        self.bin = self.tmp / "bin"
        self.dest = self.tmp / "dest"
        self.home.mkdir()
        self.env = {
            "HOME": str(self.home), "PATH": f"{self.shims}:{os.environ['PATH']}",
            "XDG_DATA_HOME": str(self.data), "XDG_CONFIG_HOME": str(self.config),
            "XDG_BIN_HOME": str(self.bin), "DESTDIR": str(self.dest),
            "SHIM_LOG": str(self.shim_log), "SSH_REPLY": "",
            "TUXISTANT_INSTALL_DRYRUN": "1",
        }

    def run_script(self, name="install.sh", *args, project=None, stdin=subprocess.DEVNULL, **env):
        return subprocess.run(["sh", str((project or self.project) / name), *args], stdin=stdin,
                              env={**self.env, **env}, capture_output=True, text=True, timeout=60)

    def ok(self, name="install.sh", *args, **env):
        done = self.run_script(name, *args, **env)
        self.assertEqual(done.returncode, 0, done.stderr)
        return done

    def real(self, name="install.sh", *args, **env):
        env.setdefault("TUXISTANT_KEY_COMMAND", "ssh keyhost get-key")
        return self.ok(name, *args, TUXISTANT_INSTALL_DRYRUN="", **env)

    def staged(self, path):
        return self.dest / str(path).lstrip("/")

    @property
    def wrapper(self):
        return self.staged(self.bin / "tuxistant")

    @property
    def extension(self):
        return self.staged(self.data / "gnome-shell/extensions" / UUID)

    @property
    def user_unit(self):
        return self.staged(self.data / "systemd/user/tuxistant.service")

    @property
    def dbus_service(self):
        return self.staged(self.data / "dbus-1/services/org.tuxistant.Daemon.service")

    @property
    def config_file(self):
        return self.staged(self.config / "tuxistant/config.json")

    @property
    def helper(self):
        return self.dest / "usr/local/lib/tuxistant/tuxistant-fn"

    @property
    def helper_unit(self):
        return self.dest / "etc/systemd/system/tuxistant-fn.service"

    def calls(self):
        return self.shim_log.read_text().splitlines() if self.shim_log.exists() else []


class DryRun(Prefix):
    def test_installs_everything_and_calls_nothing(self):
        done = self.ok()
        self.assertTrue(os.access(self.wrapper, os.X_OK))
        self.assertTrue((self.extension / "metadata.json").exists())
        self.assertTrue((self.extension / "schemas/gschemas.compiled").exists())
        self.assertTrue(self.user_unit.exists())
        self.assertTrue(self.dbus_service.exists())
        self.assertEqual(json.loads(self.config_file.read_text()), {})
        self.assertEqual(self.calls(), [])
        self.assertIn("Log out and back in once", done.stdout)
        self.assertIn(f"gnome-extensions enable {UUID}", done.stdout)

    def test_nothing_lands_outside_the_prefix(self):
        self.ok("install.sh", "--fn-helper")
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir() if p.name not in ("shims",)),
                         sorted(["dest", "home", "my project's copy"]))
        self.assertEqual(list(self.home.iterdir()), [])

    def test_config_is_private(self):
        self.ok()
        self.assertEqual(stat.S_IMODE(self.config_file.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.config_file.parent.stat().st_mode), 0o700)

    def test_wrapper_runs_the_project_with_system_python(self):
        self.ok()
        done = subprocess.run([str(self.wrapper), "hello", "two words"], capture_output=True,
                              text=True, env={"PATH": "/usr/bin:/bin"}, timeout=10, cwd=self.tmp)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn(str(self.project / "tuxistant/__init__.py"), done.stdout)
        self.assertIn("['hello', 'two words']", done.stdout)

    def test_wrapper_ignores_a_tuxistant_in_the_current_directory(self):
        self.ok()
        decoy = self.tmp / "decoy/tuxistant"
        decoy.mkdir(parents=True)
        (decoy / "__init__.py").write_text("")
        (decoy / "__main__.py").write_text("print('decoy ran')\n")
        done = subprocess.run([str(self.wrapper)], capture_output=True, text=True, timeout=10,
                              env={"PATH": "/usr/bin:/bin"}, cwd=decoy.parent)
        self.assertNotIn("decoy ran", done.stdout)
        self.assertIn(str(self.project), done.stdout)

    def test_wrapper_keeps_an_existing_pythonpath(self):
        self.ok()
        (self.tmp / "extra").mkdir()
        (self.tmp / "extra/probe_module.py").write_text("")
        (self.project / "tuxistant/__main__.py").write_text("import probe_module\nprint('both')\n")
        done = subprocess.run([str(self.wrapper)], capture_output=True, text=True, timeout=10,
                              env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(self.tmp / "extra")},
                              cwd=self.tmp)
        self.assertEqual(done.stdout.strip(), "both", done.stderr)

    def test_idempotent(self):
        self.ok("install.sh", "--fn-helper")
        first = tree(self.dest)
        self.ok("install.sh", "--fn-helper")
        self.assertEqual(tree(self.dest), first)

    def test_reinstall_keeps_settings_and_clears_stale_extension_files(self):
        self.ok()
        self.config_file.write_text('{"voice": "bm_lewis"}')
        (self.extension / "removed-upstream.js").write_text("")
        self.ok()
        self.assertEqual(json.loads(self.config_file.read_text()), {"voice": "bm_lewis"})
        self.assertFalse((self.extension / "removed-upstream.js").exists())

    def test_custom_bin_dir_reaches_the_unit_without_destdir(self):
        self.ok()
        self.assertIn(f"ExecStart={self.bin}/tuxistant daemon", self.user_unit.read_text())

    def test_default_bin_dir_keeps_the_portable_specifier(self):
        env = dict(self.env)
        del env["XDG_BIN_HOME"]
        done = subprocess.run(["sh", str(self.project / "install.sh")], env=env, text=True,
                              capture_output=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("ExecStart=%h/.local/bin/tuxistant daemon", self.user_unit.read_text())

    def test_fn_helper_is_staged(self):
        self.ok("install.sh", "--fn-helper")
        self.assertTrue(os.access(self.helper, os.X_OK))
        self.assertEqual(self.helper_unit.read_bytes(),
                         (ROOT / "helper/tuxistant-fn.service").read_bytes())
        self.assertEqual(self.calls(), [])

    def test_fn_helper_without_destdir_leaves_the_system_alone(self):
        done = self.ok("install.sh", "--fn-helper", DESTDIR="")
        self.assertIn("Dry run: skipping", done.stdout)
        self.assertEqual(self.calls(), [])

    def test_helper_is_part_of_the_default_install(self):
        self.ok()
        self.assertTrue(self.helper.exists())

    def test_no_fn_helper_skips_it_and_says_how_to_add_it(self):
        done = self.ok("install.sh", "--no-fn-helper")
        self.assertFalse(self.helper.exists())
        self.assertIn("--no-fn-helper", done.stdout)

    def test_unknown_option_changes_nothing(self):
        done = self.run_script("install.sh", "--nope")
        self.assertEqual(done.returncode, 2)
        self.assertFalse(self.dest.exists())

    def test_missing_extension_is_reported(self):
        shutil.rmtree(self.project / "extension")
        done = self.run_script()
        self.assertEqual(done.returncode, 1)
        self.assertIn("no extension", done.stderr)


class Uninstall(Prefix):
    def test_reverses_install_and_keeps_user_data(self):
        self.ok("install.sh", "--fn-helper")
        done = self.ok("uninstall.sh")
        self.assertEqual(sorted(tree(self.dest)), [str(self.config_file.relative_to(self.dest))])
        self.assertFalse(self.helper.parent.exists())
        self.assertIn("--purge", done.stdout)
        self.assertEqual(self.calls(), [])

    def test_purge_removes_user_data_too(self):
        self.ok()
        history = self.staged(self.data / "tuxistant/history.db")
        history.parent.mkdir(parents=True)
        history.write_text("x")
        self.ok("uninstall.sh", "--purge")
        self.assertEqual(tree(self.dest), {})

    def test_idempotent_and_safe_on_a_clean_system(self):
        self.ok("uninstall.sh")
        self.ok()
        self.ok("uninstall.sh")
        self.ok("uninstall.sh")

    def test_leaves_foreign_files_alone(self):
        self.ok()
        neighbour = self.extension.parent / "other@example.com/metadata.json"
        neighbour.parent.mkdir()
        neighbour.write_text("{}")
        unit = self.user_unit.parent / "other.service"
        unit.write_text("")
        self.ok("uninstall.sh")
        self.assertTrue(neighbour.exists())
        self.assertTrue(unit.exists())


class RealBranch(Prefix):
    """The non-dry path, with shims standing in for ssh, sudo and systemctl."""

    def test_key_reaches_the_config_and_nothing_else(self):
        done = self.real(SSH_REPLY=FAKE_KEY)
        self.assertEqual(json.loads(self.config_file.read_text()), {"api_key": FAKE_KEY})
        self.assertEqual(stat.S_IMODE(self.config_file.stat().st_mode), 0o600)
        self.assertNotIn(FAKE_KEY, done.stdout + done.stderr)
        self.assertNotIn(FAKE_KEY, self.shim_log.read_text())
        self.assertNotIn(FAKE_KEY, "".join(p.read_bytes().decode("latin1")
                                           for p in self.dest.rglob("*")
                                           if p.is_file() and p != self.config_file))

    def test_the_key_command_runs_once(self):
        self.real(SSH_REPLY=FAKE_KEY)
        self.assertEqual([c for c in self.calls() if c.startswith("ssh ")], ["ssh keyhost get-key"])

    def test_with_no_key_command_nothing_is_asked_of_any_server(self):
        done = self.real(TUXISTANT_KEY_COMMAND="")
        self.assertFalse([c for c in self.calls() if c.startswith("ssh ")])
        self.assertNotIn("could not fetch", done.stderr)
        self.assertEqual(json.loads(self.config_file.read_text()), {})

    def test_key_is_added_to_an_existing_config_without_one(self):
        self.config_file.parent.mkdir(parents=True)
        self.config_file.write_text('{"voice": "bm_lewis", "api_key": ""}')
        self.real(SSH_REPLY=FAKE_KEY)
        self.assertEqual(json.loads(self.config_file.read_text()),
                         {"voice": "bm_lewis", "api_key": FAKE_KEY})

    def test_existing_key_is_kept_and_ssh_is_not_used(self):
        self.config_file.parent.mkdir(parents=True)
        self.config_file.write_text('{"api_key": "mine"}')
        self.config_file.chmod(0o644)
        self.real(SSH_REPLY=FAKE_KEY)
        self.assertEqual(json.loads(self.config_file.read_text()), {"api_key": "mine"})
        self.assertEqual(stat.S_IMODE(self.config_file.stat().st_mode), 0o600)
        self.assertFalse([call for call in self.calls() if call.startswith("ssh ")])

    def test_unreachable_server_is_a_warning(self):
        done = self.real()
        self.assertIn("could not fetch the API key", done.stderr)
        self.assertEqual(json.loads(self.config_file.read_text()), {})
        self.assertIn("gnome-extensions enable", done.stdout)

    def test_broken_config_is_not_overwritten(self):
        self.config_file.parent.mkdir(parents=True)
        self.config_file.write_text("{broken")
        done = self.real(SSH_REPLY=FAKE_KEY)
        self.assertEqual(self.config_file.read_text(), "{broken")
        self.assertIn("valid JSON", done.stderr)
        self.assertNotIn("Traceback", done.stderr)
        self.assertNotIn(FAKE_KEY, done.stdout + done.stderr)

    def test_user_install_only_reloads_the_user_manager(self):
        self.real("install.sh", "--no-fn-helper", SSH_REPLY=FAKE_KEY)
        self.assertEqual([c for c in self.calls() if not c.startswith("ssh ")],
                         ["systemctl --user daemon-reload"])

    def test_fn_helper_enables_and_restarts_through_sudo(self):
        self.real("install.sh", "--fn-helper", SSH_REPLY=FAKE_KEY)
        calls = self.calls()
        self.assertTrue(self.helper.exists())
        self.assertLess(calls.index("sudo systemctl enable tuxistant-fn.service"),
                        calls.index("sudo systemctl restart tuxistant-fn.service"))
        self.assertIn("sudo systemctl daemon-reload", calls)
        self.assertIn("systemctl --user enable tuxistant.service", calls)
        self.assertTrue(any(c.startswith("sudo install") and "tuxistant-fn" in c for c in calls))

    def test_never_runs_gnome_extensions_or_a_login_step(self):
        self.real("install.sh", "--fn-helper", SSH_REPLY=FAKE_KEY)
        self.assertFalse([c for c in self.calls() if "gnome-extensions" in c or "loginctl" in c])

    def test_uninstall_stops_both_services(self):
        self.real("install.sh", "--fn-helper", SSH_REPLY=FAKE_KEY)
        self.shim_log.unlink()
        self.real("uninstall.sh")
        calls = self.calls()
        self.assertIn("systemctl --user disable --now tuxistant.service", calls)
        self.assertIn("sudo systemctl disable --now tuxistant-fn.service", calls)
        self.assertFalse(self.helper_unit.exists())

    def test_uninstall_without_the_helper_never_asks_for_sudo(self):
        self.real("install.sh", "--no-fn-helper", SSH_REPLY=FAKE_KEY)
        self.shim_log.unlink()
        self.real("uninstall.sh")
        self.assertFalse([c for c in self.calls() if c.startswith("sudo ")])


def sdk_wheel(folder, version="1.0.0", body="WORKS = True\n"):
    """A wheel called anthropic, so the real pip can install it with no network."""
    info = f"anthropic-{version}.dist-info"
    with zipfile.ZipFile(folder / f"anthropic-{version}-py3-none-any.whl", "w") as wheel:
        wheel.writestr("anthropic/__init__.py", body)
        wheel.writestr(f"{info}/METADATA",
                       f"Metadata-Version: 2.1\nName: anthropic\nVersion: {version}\n")
        wheel.writestr(f"{info}/WHEEL", "Wheel-Version: 1.0\nGenerator: test\n"
                                        "Root-Is-Purelib: true\nTag: py3-none-any\n")
        wheel.writestr(f"{info}/RECORD", "")


def has_pip():
    return subprocess.run(["/usr/bin/python3", "-P", "-m", "pip", "--version"],
                          capture_output=True).returncode == 0


class CloudSdk(Prefix):
    """The real pip, reading a local folder of wheels. PIP_NO_INDEX keeps it off the network."""

    def setUp(self):
        super().setUp()
        self.wheels = self.tmp / "wheels"
        self.wheels.mkdir()
        self.env |= {"PIP_NO_INDEX": "1", "PIP_FIND_LINKS": str(self.wheels),
                     "PIP_DISABLE_PIP_VERSION_CHECK": "1", "TUXISTANT_INSTALL_DRYRUN": ""}

    @property
    def vendor(self):
        return self.staged(self.data / "tuxistant/vendor")

    def at_a_terminal(self, typed):
        """Runs install.sh with a terminal for stdin, where the answer is already waiting."""
        ours, theirs = pty.openpty()
        self.addCleanup(os.close, ours)
        self.addCleanup(os.close, theirs)
        os.write(ours, typed)
        return self.ok("install.sh", "--no-fn-helper", stdin=theirs)

    def test_a_dry_run_leaves_it_alone_even_when_asked(self):
        done = self.ok("install.sh", "--cloud-sdk", TUXISTANT_INSTALL_DRYRUN="1")
        self.assertIn("Dry run: skipping the Claude SDK", done.stdout)
        self.assertFalse(self.vendor.exists())

    def test_without_a_terminal_it_only_says_how(self):
        sdk_wheel(self.wheels)
        done = self.ok("install.sh", "--no-fn-helper")
        self.assertIn("--cloud-sdk", done.stdout)
        self.assertFalse(self.vendor.exists())

    @unittest.skipUnless(has_pip(), "pip is not installed")
    def test_the_flag_installs_it_in_its_own_folder_and_nowhere_else(self):
        sdk_wheel(self.wheels)
        done = self.ok("install.sh", "--no-fn-helper", "--cloud-sdk")
        self.assertTrue((self.vendor / "anthropic/__init__.py").exists(), done.stderr)
        self.assertFalse((self.home / ".local").exists())

    @unittest.skipUnless(has_pip(), "pip is not installed")
    def test_the_daemons_python_can_import_what_landed(self):
        sdk_wheel(self.wheels)
        self.ok("install.sh", "--no-fn-helper", "--cloud-sdk")
        check = "import anthropic; print(anthropic.WORKS)"
        done = subprocess.run(["/usr/bin/python3", "-P", "-c", check], cwd=self.tmp,
                              env={"PYTHONPATH": str(self.vendor)}, capture_output=True, text=True,
                              timeout=10)
        self.assertEqual(done.stdout.strip(), "True", done.stderr)

    @unittest.skipUnless(has_pip(), "pip is not installed")
    def test_saying_yes_at_the_terminal_installs_it(self):
        sdk_wheel(self.wheels)
        done = self.at_a_terminal(b"y\n")
        self.assertIn("Add the Claude SDK", done.stdout)
        self.assertTrue((self.vendor / "anthropic/__init__.py").exists(), done.stderr)

    def test_anything_else_at_the_terminal_is_a_no(self):
        sdk_wheel(self.wheels)
        for typed in (b"n\n", b"\n", b"nope\n"):
            with self.subTest(typed):
                self.at_a_terminal(typed)
                self.assertFalse(self.vendor.exists())

    def test_a_failed_download_is_a_warning_and_the_install_still_finishes(self):
        done = self.ok("install.sh", "--no-fn-helper", "--cloud-sdk")     # no wheels to be found
        self.assertIn("could not add the Claude SDK", done.stderr)
        self.assertIn("gnome-extensions enable", done.stdout)
        self.assertTrue(os.access(self.wrapper, os.X_OK))

    def test_no_pip_at_all_is_a_warning_too(self):
        (self.tmp / "nopip/pip").mkdir(parents=True)
        (self.tmp / "nopip/pip/__main__.py").write_text("raise SystemExit('no module named pip')\n")
        done = self.ok("install.sh", "--no-fn-helper", "--cloud-sdk",
                       PYTHONPATH=str(self.tmp / "nopip"))
        self.assertIn("could not add the Claude SDK", done.stderr)
        self.assertIn("gnome-extensions enable", done.stdout)

    @unittest.skipUnless(has_pip(), "pip is not installed")
    def test_a_working_copy_is_left_alone(self):
        sdk_wheel(self.wheels)
        self.ok("install.sh", "--no-fn-helper", "--cloud-sdk")
        (self.wheels / "anthropic-1.0.0-py3-none-any.whl").unlink()
        done = self.ok("install.sh", "--no-fn-helper", "--cloud-sdk")   # pip would fail now
        self.assertNotIn("could not add", done.stderr)
        self.assertTrue((self.vendor / "anthropic/__init__.py").exists())

    @unittest.skipUnless(has_pip(), "pip is not installed")
    def test_a_broken_copy_is_replaced(self):
        """The planner tells the user to run this again, so that has to mend it."""
        sdk_wheel(self.wheels, "1.0.0", "raise ImportError('built for another Python')\n")
        self.ok("install.sh", "--no-fn-helper", "--cloud-sdk")
        sdk_wheel(self.wheels, "2.0.0")
        done = self.ok("install.sh", "--no-fn-helper", "--cloud-sdk")
        self.assertIn("WORKS", (self.vendor / "anthropic/__init__.py").read_text(), done.stderr)

    @unittest.skipUnless(has_pip(), "pip is not installed")
    def test_purge_takes_it_away(self):
        sdk_wheel(self.wheels)
        self.ok("install.sh", "--no-fn-helper", "--cloud-sdk")
        self.ok("uninstall.sh", "--purge")
        self.assertFalse(self.vendor.exists())


class RealTree(Prefix):
    def test_this_checkout_installs(self):
        if not (ROOT / "extension" / UUID / "metadata.json").exists():
            self.skipTest("the extension is not there yet")
        done = self.run_script("install.sh", "--fn-helper", project=ROOT)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertTrue((self.extension / "metadata.json").exists())
        self.assertIn(f"'{ROOT}'", self.wrapper.read_text())
        ran = subprocess.run([str(self.wrapper), "--help"], capture_output=True, text=True,
                             timeout=30, cwd=self.tmp,  # the real HOME finds pip --user modules
                             env={"PATH": "/usr/bin:/bin", "HOME": os.environ["HOME"]})
        self.assertEqual(ran.returncode, 0, ran.stderr)
        self.assertIn("daemon", ran.stdout)  # the subcommand tuxistant.service runs


class Syntax(unittest.TestCase):
    SCRIPTS = ("install.sh", "uninstall.sh", "packaging/paths.sh")

    def test_posix_shell_syntax(self):
        for script in self.SCRIPTS:
            done = subprocess.run(["sh", "-n", str(ROOT / script)], capture_output=True, text=True)
            self.assertEqual(done.returncode, 0, f"{script}: {done.stderr}")

    @unittest.skipUnless(shutil.which("shellcheck"), "shellcheck is not installed")
    def test_shellcheck(self):
        done = subprocess.run(["shellcheck", "-x", *self.SCRIPTS], cwd=ROOT, capture_output=True,
                              text=True)
        self.assertEqual(done.returncode, 0, done.stdout)

    def test_scripts_are_executable(self):
        for script in ("install.sh", "uninstall.sh"):
            self.assertTrue(os.access(ROOT / script, os.X_OK), script)


def unit(path):
    parser = configparser.RawConfigParser(strict=False, delimiters=("=",))
    parser.optionxform = str
    parser.read(path)
    return parser


class Units(unittest.TestCase):
    def test_user_unit(self):
        service = unit(ROOT / "packaging/tuxistant.service")["Service"]
        self.assertEqual(service["Type"], "dbus")
        self.assertEqual(service["BusName"], "org.tuxistant.Daemon")

    def test_daemon_memory_is_capped(self):
        self.assertIn("MemoryMax", unit(ROOT / "packaging/tuxistant.service")["Service"])

    def test_python_never_imports_from_the_current_folder(self):
        for line in (ROOT / "install.sh").read_text().splitlines():
            if "/usr/bin/python3 " in line and "PYTHONPATH" not in line:
                self.assertIn("/usr/bin/python3 -P ", line)

    def test_activation_file(self):
        service = unit(ROOT / "packaging/org.tuxistant.Daemon.service")["D-BUS Service"]
        self.assertEqual(service["Name"], "org.tuxistant.Daemon")
        self.assertEqual(service["SystemdService"], "tuxistant.service")

    def test_names_match_the_python_package(self):
        import tuxistant
        self.assertEqual(tuxistant.BUS_NAME, "org.tuxistant.Daemon")

    def test_helper_sandbox(self):
        service = unit(ROOT / "helper/tuxistant-fn.service")["Service"]
        expected = {
            "DynamicUser": "yes", "SupplementaryGroups": "input",
            "RuntimeDirectory": "tuxistant-fn", "RuntimeDirectoryMode": "0755",
            "NoNewPrivileges": "yes", "PrivateNetwork": "yes",
            "RestrictAddressFamilies": "AF_UNIX", "ProtectSystem": "strict", "ProtectHome": "yes",
            "MemoryDenyWriteExecute": "yes", "PrivateDevices": "no", "DevicePolicy": "closed",
            "DeviceAllow": "char-input r", "CapabilityBoundingSet": "", "LimitCORE": "0",
        }
        for name, value in expected.items():
            self.assertEqual(service.get(name), value, name)
        self.assertNotIn("ProtectClock", service)  # it would add char-rtc to the device list
        text = (ROOT / "helper/tuxistant-fn.service").read_text()
        self.assertIn("SystemCallFilter=~connect\n", text)     # host sockets are the way out
        self.assertIn("InaccessiblePaths=-/dev/shm\n", text)

    def test_helper_unit_matches_the_contract_socket(self):
        source = (ROOT / "helper/tuxistant-fn").read_text()
        self.assertIn('SOCKET = "/run/tuxistant-fn/events.sock"', source)
        self.assertIn("/usr/local/lib/tuxistant/tuxistant-fn",
                      (ROOT / "helper/tuxistant-fn.service").read_text())

    @unittest.skipUnless(shutil.which("systemd-analyze"), "systemd-analyze is not installed")
    def test_systemd_accepts_the_units(self):
        with tempfile.TemporaryDirectory() as tmp:
            # verify insists that ExecStart exists, so aim it at a real file.
            exe = Path(tmp, "exe")
            exe.write_text("#!/bin/sh\n")
            exe.chmod(0o755)
            for source, flags in ((ROOT / "helper/tuxistant-fn.service", []),
                                  (ROOT / "packaging/tuxistant.service", ["--user"])):
                copy = Path(tmp, source.name)
                copy.write_text(re.sub(r"^ExecStart=\S+", f"ExecStart={exe}", source.read_text(),
                                       flags=re.M))
                done = subprocess.run(["systemd-analyze", *flags, "verify", "--man=no", str(copy)],
                                      capture_output=True, text=True, timeout=60)
                self.assertEqual(done.returncode, 0, done.stderr)
                self.assertNotIn(source.name, done.stderr)

    @unittest.skipUnless(shutil.which("systemd-analyze"), "systemd-analyze is not installed")
    def test_helper_exposure_stays_low(self):
        done = subprocess.run(["systemd-analyze", "security", "--offline=true", "--no-pager",
                               str(ROOT / "helper/tuxistant-fn.service")],
                              capture_output=True, text=True, timeout=60)
        match = re.search(r"Overall exposure level for \S+: (\d+\.\d+)", done.stdout)
        self.assertIsNotNone(match, done.stdout + done.stderr)
        self.assertLess(float(match[1]), 1.5)


def fences(text, language):
    return re.findall(rf"```{language}\n(.*?)```", text, re.S)


class Docs(unittest.TestCase):
    def test_theme_example_passes_validation(self):
        from tuxistant import themes
        text = (ROOT / "docs/THEMES.md").read_text()
        meta = json.loads(next(f for f in fences(text, "json") if '"layout"' in f))
        shader = next(f for f in fences(text, "glsl") if "vec4 tux_theme" in f)
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp, meta["id"])
            folder.mkdir()
            (folder / "theme.json").write_text(json.dumps(meta))
            (folder / "shader.glsl").write_text(shader)
            self.assertEqual(themes.validate(folder)["id"], meta["id"])

    def test_uniform_table_matches_the_contract(self):
        contract = (ROOT / "docs/CONTRACT.md").read_text()
        uniforms = re.findall(r"uniform (\w+)\s+(u_\w+);", contract)
        self.assertTrue(uniforms)
        text = (ROOT / "docs/THEMES.md").read_text()
        for kind, name in uniforms:
            self.assertRegex(text, rf"\| `{name}` \| `{kind}` \|")

    def test_skill_example_loads_and_risk_ignores_the_manifest(self):
        import yaml
        from tuxistant import skills
        text = (ROOT / "docs/SKILLS.md").read_text()
        manifests = fences(text, "yaml")
        self.assertTrue(manifests)
        with tempfile.TemporaryDirectory() as tmp:
            for index, manifest in enumerate(manifests):
                self.assertNotIn("risk", yaml.safe_load(manifest))
                Path(tmp, f"{index}.yaml").write_text(manifest + "risk: low\n")  # a false label
            loaded = skills.Skills(Path(tmp))
            self.assertEqual({s["risk"] for s in loaded.skills.values()}, {"medium"})
            self.assertEqual(len(loaded.tools()), len(manifests))

    def test_docs_mark_planned_work(self):
        text = (ROOT / "docs/SKILLS.md").read_text().lower()
        self.assertIn("planned", text)
        self.assertIn("never", text)

    def test_docs_credit_original_artwork(self):
        self.assertIn("drawn from scratch", (ROOT / "docs/THEMES.md").read_text())


if __name__ == "__main__":
    unittest.main()
