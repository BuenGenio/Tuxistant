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
import site
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
        self.state = self.tmp / "state"
        self.bin = self.tmp / "bin"
        self.dest = self.tmp / "dest"
        self.home.mkdir()
        self.env = {
            "HOME": str(self.home), "PATH": f"{self.shims}:{os.environ['PATH']}",
            "XDG_DATA_HOME": str(self.data), "XDG_CONFIG_HOME": str(self.config),
            "XDG_BIN_HOME": str(self.bin), "XDG_STATE_HOME": str(self.state),
            "DESTDIR": str(self.dest),
            "SHIM_LOG": str(self.shim_log), "SSH_REPLY": "",
            "TUXISTANT_INSTALL_DRYRUN": "1",
            # HOME is a temp folder, and the daemon's modules may be in the real user's site folder.
            "PYTHONUSERBASE": site.getuserbase(),
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
        # An update leaves the old daemon running, with its own rules for what goes to Claude.
        self.assertIn("restarts the daemon too", done.stdout)
        self.assertIn("\n     Cloud rules and all. To restart only the daemon now: "
                      "systemctl --user restart tuxistant\n", done.stdout)

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

    def test_a_missing_module_stops_it_before_anything_is_copied(self):
        # None in sys.modules is how Python records a module that can't be imported.
        (self.tmp / "without").mkdir()
        (self.tmp / "without/sitecustomize.py").write_text(
            "import sys\nsys.modules['dbus_fast'] = sys.modules['websockets'] = None\n")
        done = self.run_script("install.sh", "--no-fn-helper", PYTHONPATH=str(self.tmp / "without"))
        self.assertEqual(done.returncode, 1)
        self.assertIn("sudo apt install python3-dbus-fast python3-websockets\n", done.stderr)
        self.assertNotIn("python3-yaml", done.stderr, "only what is missing is named")
        self.assertNotIn("is installed", done.stdout)
        self.assertFalse(self.dest.exists())

    def test_missing_extension_is_reported(self):
        shutil.rmtree(self.project / "extension")
        done = self.run_script()
        self.assertEqual(done.returncode, 1)
        self.assertIn("no extension", done.stderr)


class Uninstall(Prefix):
    def skill_state(self):
        kept = self.staged(self.state / "tuxistant/skills/lights/lights.json")
        kept.parent.mkdir(parents=True)
        kept.write_text("{}")
        return kept

    def test_reverses_install_and_keeps_user_data(self):
        self.ok("install.sh", "--fn-helper")
        kept = self.skill_state()
        done = self.ok("uninstall.sh")
        self.assertEqual(sorted(tree(self.dest)), sorted(
            str(p.relative_to(self.dest)) for p in (self.config_file, kept)))
        self.assertFalse(self.helper.parent.exists())
        self.assertIn("--purge", done.stdout)
        self.assertIn(str(self.staged(self.state / "tuxistant")), done.stdout)
        self.assertEqual(self.calls(), [])

    def test_purge_removes_user_data_too(self):
        self.ok()
        history = self.staged(self.data / "tuxistant/history.db")
        history.parent.mkdir(parents=True)
        history.write_text("x")
        self.skill_state()
        self.ok("uninstall.sh", "--purge")
        self.assertEqual(tree(self.dest), {})
        self.assertFalse(self.staged(self.state / "tuxistant").exists())

    def test_state_dir_is_where_code_skills_keep_their_state(self):
        for xdg in ({"XDG_STATE_HOME": str(self.state)}, {}, {"XDG_STATE_HOME": ""}):
            env = {"HOME": str(self.home), "PATH": os.environ["PATH"], **xdg}
            shell = subprocess.run(
                ["sh", "-c", '. "$PROJECT/packaging/paths.sh" && printf %s "$STATE_DIR"'],
                env={**env, "PROJECT": str(ROOT)}, capture_output=True, text=True, check=True)
            python = subprocess.run(
                [sys.executable, "-c", "from tuxistant import skillbox, skillhost\n"
                 "print(skillbox.state_dir('lights')); print(skillhost._state_dir('lights'))"],
                env={**env, "PYTHONPATH": str(ROOT), "PYTHONUSERBASE": site.getuserbase()},
                cwd=self.tmp, capture_output=True, text=True, timeout=60)
            self.assertEqual(python.returncode, 0, python.stderr)
            expected = Path(shell.stdout) / "skills" / "lights"
            self.assertTrue(expected.is_relative_to(self.state if xdg.get("XDG_STATE_HOME") else self.home),
                            expected)
            self.assertEqual(python.stdout.split(), [str(expected)] * 2)

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

    def test_user_install_only_reloads_the_user_manager_and_starts_the_daemon_at_login(self):
        """With no Fn helper too: a reminder set yesterday needs a daemon running today."""
        self.real("install.sh", "--no-fn-helper", SSH_REPLY=FAKE_KEY)
        self.assertEqual([c for c in self.calls() if not c.startswith("ssh ")],
                         ["systemctl --user daemon-reload",
                          "systemctl --user enable tuxistant.service"])

    def test_fn_helper_enables_and_restarts_through_sudo(self):
        self.real("install.sh", "--fn-helper", SSH_REPLY=FAKE_KEY)
        calls = self.calls()
        self.assertTrue(self.helper.exists())
        self.assertLess(calls.index("sudo systemctl enable tuxistant-fn.service"),
                        calls.index("sudo systemctl restart tuxistant-fn.service"))
        self.assertIn("sudo systemctl daemon-reload", calls)
        self.assertEqual(calls.count("systemctl --user enable tuxistant.service"), 1)
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
        (self.tmp / "nopip/pip/__init__.py").write_text("")     # or the real pip is found first
        (self.tmp / "nopip/pip/__main__.py").write_text("raise SystemExit('no module named pip')\n")
        done = self.ok("install.sh", "--no-fn-helper", "--cloud-sdk",
                       PYTHONPATH=str(self.tmp / "nopip"))
        self.assertIn("needs pip", done.stderr)
        self.assertIn("python3-pip", done.stderr)
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
                             env={"PATH": "/usr/bin:/bin", "HOME": os.environ["HOME"],
                                  "PYTHONUSERBASE": site.getuserbase()})
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

    def test_skills_md_names_every_first_word_a_skill_you_add_cant_use(self):
        from tuxistant import skillhost
        rules = next(line for line in (ROOT / "docs/SKILLS.md").read_text().splitlines()
                     if line.startswith("**What it can't do.**"))
        listed = re.search(r"Those first words are (.*?)\.", rules)
        self.assertIsNotNone(listed, "SKILLS.md lists them, as 0.4.0 skills can trip on them")
        named = re.findall(r"`([a-z]+)`", listed[1])
        words = {skillhost._namespace(word) for word in named}
        self.assertEqual(words, built_in_first_words())
        stripped = {word for word in words if word not in named}
        self.assertEqual(stripped, set(), "a word refused as it is compared is named as well")
        # and the host refuses exactly those, judged as for a code skill but with no sandbox
        with tempfile.TemporaryDirectory() as tmp:
            user = Path(tmp, "skills")
            user.mkdir()
            for word in sorted(words) + ["zebra"]:
                for form in (word, word + "s"):
                    (user / f"{form}-{word}.yaml").write_text(
                        f"id: org.example.{form}\nname: X\ndescription: Y\npermissions: {{}}\n"
                        f"actions:\n  {form}_extra.get:\n    description: Get\n    say: Hi\n")
            problems = skillhost.offline({}, user=user).problems
            refused = {Path(key).stem for key, why in problems.items() if "starts like" in why}
            self.assertEqual(refused, {f"{form}-{word}" for word in words
                                       for form in (word, word + "s")})
            self.assertEqual(set(problems), {str(user / f"{name}.yaml") for name in refused})

    def test_each_release_says_what_changes_for_the_one_before_and_what_to_do(self):
        sections = changelog_sections()
        for (heading, body), (older, _) in zip(sections, sections[1:]):
            older = re.match(r"\d+\.\d+\.\d+", older)
            if older is None or tuple(map(int, older[0].split("."))) < (0, 2, 0):
                continue
            with self.subTest(heading):
                self.assertRegex(
                    body, rf"(?m)^\w+ changes? reach(es)? what {re.escape(older[0])} already did:$")
                self.assertRegex(body, r"(?m)^After updating, ")

    def test_the_upgrade_note_on_code_skills_matches_the_rules(self):
        from tuxistant import skillhost
        body = next(body for _, body in changelog_sections()
                    if "reach what 0.4.0 already did" in body)
        note = next(line for line in body.splitlines() if "loaded in 0.4.0" in line)
        words = re.findall(r"`([a-z]+)`", re.search(r"may not be (.*?), so ", note)[1])
        self.assertGreater(len(words), 1)
        self.assertEqual({skillhost._namespace(word) for word in words}, built_in_first_words())
        self.assertLessEqual({skillhost._namespace(word) for word in words}, set(words),
                             "each word is named as the rule compares it, so `new` beside `news`")
        self.assertEqual(skillhost.USER_TOOLS, 6)
        self.assertIn("six actions", note)
        self.assertIn("anchors", note)
        self.assertIn('"Skills with problems"', note)
        self.assertIn('"Skills with problems"', (ROOT / "tuxistant/settings_app.py").read_text())

    def test_what_the_docs_say_of_the_budget_holds_without_a_note_of_what_loaded(self):
        """0.4.0 wrote no skills-loaded.json, so at the first start after it every skill is new:
        one that was working can be refused, and one past USER_TOOLS alone is refused anyway."""
        from tuxistant import skillhost
        sizes = {"a": 4, "b": 3, "c": 2, "d": skillhost.USER_TOOLS + 1}
        with tempfile.TemporaryDirectory() as tmp:
            user = Path(tmp, "skills")
            for name, count in sizes.items():
                folder = user / name
                folder.mkdir(parents=True)
                (folder / "main.py").write_text("")
                (folder / skillhost.MANIFEST).write_text(
                    f"id: org.example.{name}\nname: X\ndescription: Y\nrun: [python3, main.py]\n"
                    "actions:\n" + "".join(f"  {name}x.a{i}:\n    description: Do\n"
                                            for i in range(count)))
            self.assertFalse((user.parent / skillhost.LOADED).exists())
            problems = skillhost.offline({}, user=user).problems
        self.assertEqual(set(problems), {str(user / "b"), str(user / "d")},
                         "b worked in 0.4.0, c still fits, d is too big alone")
        self.assertTrue(problems[str(user / "d")].startswith("It has 7 actions"))
        self.assertIn("some of its own", problems[str(user / "d")], "taking others out won't do")
        seven = "seven or more"
        self.assertEqual(skillhost.USER_TOOLS + 1, 7)
        rule = next(line for line in (ROOT / "docs/SKILLS.md").read_text().splitlines()
                    if line.startswith("**Six actions in all.**"))
        self.assertIn(skillhost.LOADED, rule)
        self.assertNotIn("never one that was working", rule)
        self.assertIn("first start after updating from 0.4.0", rule)
        self.assertIn("one that was working can be refused", rule)
        self.assertIn(f"One skill with {seven} actions is refused", rule)
        body = changelog_sections()[0][1]
        entry = next(line for line in body.splitlines()
                     if line.startswith("- Skills you add, code skills and recipes together"))
        self.assertIn("first start after 0.4.0", entry)
        self.assertIn(f"One skill with {seven} actions is refused", entry)
        note = next(line for line in body.splitlines() if "loaded in 0.4.0" in line)
        self.assertIn(f"for one with {seven}", note, "taking another out can't bring it back")

    def test_the_budget_contract_names_only_what_is_there(self):
        """It named _held_by_another() after that was gone."""
        from tuxistant import skillhost
        rule = next(line for line in (ROOT / "docs/CONTRACT.md").read_text().splitlines()
                    if line.startswith("- An action of yours may not start like"))
        named = set(re.findall(r"`(\w+)\(\)`", rule))
        self.assertGreater(len(named), 10)
        homes = (skillhost, skillhost.SkillHost, skillhost._Grants)
        self.assertEqual({name for name in named
                          if not any(hasattr(home, name) for home in homes)}, set())

    def test_a_reach_list_counts_its_items(self):
        numbers = {"One": 1, "Two": 2, "Three": 3, "Four": 4, "Five": 5, "Six": 6}
        counted = 0
        for heading, body in changelog_sections():
            said = re.search(r"(?m)^(\w+) changes? reach(?:es)? what [\d.]+ already did:\n\n"
                             r"((?:- .*\n)+)", body)
            if said:
                counted += 1
                with self.subTest(heading):
                    self.assertEqual(numbers[said[1]], said[2].count("\n"))
        self.assertGreaterEqual(counted, 3)

    def test_the_reach_list_names_follow_ups_carrying_a_conversation_over(self):
        from tuxistant import controller
        body = changelog_sections()[0][1]
        reach = body.split("already did:\n\n", 1)[1].split("\n\n", 1)[0]
        self.assertEqual(controller.FOLLOW_ON_S, 120)
        item = next(line for line in reach.splitlines() if "Fn hold within 2 minutes" in line)
        self.assertIn("card", item)
        # Only what reads_private calls private makes a later request need a card, so each
        # thing the item says a hold read has to be one of those.
        from tuxistant import skills
        actions = skills.Skills().actions
        reads = {"mail": "mail.search", "files": "file.find", "calendar": "calendar.events",
                 "selected text": "translate.selection", "command output": "shell.run",
                 "home": "home.status", "the web": "web.search", "research": "research.run",
                 "the news": "news.headlines", "the weather": "weather.forecast"}
        said = re.search(r"after a hold that read (?:your )?(.+?)\.", item)
        self.assertTrue(said, item)
        for source in re.split(r", (?:or )?| or ", said[1]):
            with self.subTest(source):
                self.assertIn(source, reads, "name a source this test knows")
                self.assertTrue(skills.reads_private(actions[reads[source]]["permissions"]))

    def test_the_security_table_gives_each_protection_one_status(self):
        readme = (ROOT / "README.md").read_text()
        table = readme.split("| Protection | Status |\n", 1)[1].split("\n\n", 1)[0]
        seen = {}
        for row in table.splitlines()[1:]:
            protection = row.split("|")[1].strip()
            for topic in re.split(r", | and ", protection):
                self.assertNotIn(topic.casefold(), seen, f"{protection!r} and "
                                 f"{seen.get(topic.casefold())!r}")
                seen[topic.casefold()] = protection

    def test_readme_says_every_kind_of_thing_a_recipe_can_do(self):
        from tuxistant import recipes
        said = {"get": "asks a host", "post": "asks a host", "open": "opens an app",
                "desktop": "desktop actions", "say": "says a sentence"}
        not_effects = {"description", "params", "required", "json", "text", "pick"}
        sentence = next(line for line in (ROOT / "README.md").read_text().splitlines()
                        if "Or write a recipe" in line)
        for effect in recipes.ACTION_FIELDS - not_effects:
            with self.subTest(effect):
                self.assertIn(said[effect], sentence)

    def test_readme_counts_the_tests_it_names_as_the_suite_does(self):
        readme = (ROOT / "README.md").read_text()
        counted = re.findall(r"\[tests/(test_\w+)\.py\]\(tests/\1\.py\) (?:has|keeps) (\d+)", readme)
        self.assertGreaterEqual(len(counted), 2)
        for module, count in counted:
            with self.subTest(module):
                suite = unittest.defaultTestLoader.loadTestsFromName(f"tests.{module}")
                self.assertEqual(suite.countTestCases(), int(count))

    def test_the_changelog_says_where_a_model_name_is_refused_as_the_code_does(self):
        from tuxistant import config
        text = (ROOT / "CHANGELOG.md").read_text()
        sentence = next(s for s in re.split(r"(?<=\.) ", text) if config.NOT_A_MODEL in s)
        self.assertIn(f"{config.MAX_MODEL_CHARS} characters", sentence)
        self.assertFalse(config.valid_model(" claude-x "), "D-Bus refuses the spaces")
        apply = re.search(r"def on_model_apply\(self, row\):\n(.*)\n",
                          (ROOT / "tuxistant/settings_app.py").read_text())[1]
        self.assertIn("get_text().strip()", apply, "Settings trims them")
        self.assertIn("Settings trims", sentence)

    def test_a_switch_the_newest_entry_announces_is_marked_untried_and_has_a_check(self):
        labels = re.findall(r'_switch\("\w+", "([^"]+)"',
                            (ROOT / "tuxistant/settings_app.py").read_text())
        self.assertIn("Say my Tailscale address", labels)
        roadmap = (ROOT / "docs/ROADMAP.md").read_text()
        checks = roadmap.split("## Check on the real desktop", 1)[1].split("\n## ", 1)[0]
        for bullet in re.findall(r"(?m)^- .*$", changelog_sections()[0][1]):
            for label in labels:
                if f'"{label.casefold()}"' in bullet.casefold():
                    with self.subTest(label):
                        self.assertIn("not yet tried on a real desktop", bullet.casefold())
                        self.assertIn(label.casefold(), checks.casefold())

    def test_the_real_desktop_checks_are_numbered_in_order(self):
        roadmap = (ROOT / "docs/ROADMAP.md").read_text()
        checks = roadmap.split("## Check on the real desktop", 1)[1].split("\n## ", 1)[0]
        numbers = [int(n) for n in re.findall(r"(?m)^(\d+)\. ", checks)]
        self.assertEqual(numbers, list(range(1, len(numbers) + 1)))


def changelog_sections():
    """(heading, body) for each "## " section of CHANGELOG.md, newest first."""
    parts = re.split(r"(?m)^## (.+)$", (ROOT / "CHANGELOG.md").read_text())
    return list(zip(parts[1::2], parts[2::2]))


def built_in_first_words():
    """What _resolve calls ours: the first word of every built-in and bundled action, as
    _namespace reads it."""
    import yaml
    from tuxistant import skillhost
    from tuxistant.skills import BUILTIN, HANDLERS
    names = set(HANDLERS)
    for file in BUILTIN.glob("*.yaml"):
        names |= set(yaml.safe_load(file.read_text())["actions"])
    for folder in skillhost.BUNDLED.iterdir():
        if (folder / skillhost.MANIFEST).is_file():
            names |= set(skillhost.load_code_manifest(folder, True)["actions"])
    return {skillhost._namespace(name) for name in names}


if __name__ == "__main__":
    unittest.main()
