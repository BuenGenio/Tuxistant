"""Theme validation and discovery."""
import ctypes.util
import json
import os
import string
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tuxistant import themes  # noqa: E402

SHADER = "vec4 tux_theme(vec2 uv) { return vec4(uv, 0.0, 1.0); }\n"
ORB = {"kind": "orb", "size": 220, "anchor": "bottom-right", "margin": 32}


def write(root: Path, name: str, meta=None, shader=SHADER) -> Path:
    folder = root / name
    folder.mkdir()
    if meta is None:
        meta = {"id": name, "name": name.title(), "author": "Me", "version": "1.0.0", "layout": ORB}
    (folder / "theme.json").write_text(meta if isinstance(meta, str) else json.dumps(meta))
    if shader is not None:
        (folder / "shader.glsl").write_text(shader)
    return folder


class Validate(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def meta(self, **changes):
        return {"id": "t", "name": "T", "author": "Me", "version": "1.0.0", "layout": ORB, **changes}

    def test_good_orb(self):
        meta = themes.validate(write(self.root, "good"))
        self.assertEqual(meta["id"], "good")
        self.assertEqual(meta["path"], str(self.root / "good"))

    def test_good_band(self):
        themes.validate(write(self.root, "t", self.meta(layout={"kind": "band", "height": 0.1})))

    def test_shader_comments_may_mention_what_code_may_not_use(self):
        shader = "// no texture() or #version here\n/* sampler2D\n */\n" + SHADER
        themes.validate(write(self.root, "t", self.meta(), shader))

    def test_rejected(self):
        layouts = {
            "orb too small": {**ORB, "size": 10},
            "orb too big": {**ORB, "size": 5000},
            "unknown anchor": {**ORB, "anchor": "middle"},
            "orb too short": {**ORB, "height": 8},
            "unknown kind": {"kind": "square"},
            "no layout": None,
            "band too thin": {"kind": "band", "height": 0.01},
            "band too tall": {"kind": "band", "height": 1.5},
        }
        for reason, layout in layouts.items():
            with self.subTest(reason), tempfile.TemporaryDirectory() as root:
                with self.assertRaises((ValueError, TypeError, KeyError)):
                    themes.validate(write(Path(root), "t", self.meta(layout=layout)))

    def test_id_rules(self):
        for bad in ("Upper", "has space", "-dash", "../up", ""):
            with self.subTest(bad), tempfile.TemporaryDirectory() as root:
                with self.assertRaisesRegex(ValueError, "id must"):
                    themes.validate(write(Path(root), "t", self.meta(id=bad)))

    def test_id_must_equal_folder(self):
        with self.assertRaisesRegex(ValueError, "folder"):
            themes.validate(write(self.root, "t", self.meta(id="other")))

    def test_required_fields(self):
        for key in ("name", "author", "version"):
            meta = self.meta()
            del meta[key]
            with self.subTest(key), tempfile.TemporaryDirectory() as root:
                with self.assertRaisesRegex(ValueError, key):
                    themes.validate(write(Path(root), "t", meta))

    def test_shader_rules(self):
        cases = {
            "#version 330\n" + SHADER: "version",
            "# version 330\n" + SHADER: "version",
            '#include "x"\n' + SHADER: "include",
            "uniform sampler2D s;\n" + SHADER: "sampler",
            SHADER + "vec4 f(vec2 p) { return texture2D(s, p); }": "texture",
            "vec4 other(vec2 uv) { return vec4(0.0); }": "tux_theme",
            "// vec4 tux_theme(vec2 uv)\n": "tux_theme",
        }
        for shader, error in cases.items():
            with self.subTest(error), tempfile.TemporaryDirectory() as root:
                with self.assertRaisesRegex(ValueError, error):
                    themes.validate(write(Path(root), "t", self.meta(), shader))

    def test_shader_size_limit(self):
        shader = SHADER + "//" + "x" * themes.MAX_SHADER_BYTES
        with self.assertRaisesRegex(ValueError, "too large"):
            themes.validate(write(self.root, "t", self.meta(), shader))

    def test_missing_shader(self):
        with self.assertRaises(OSError):
            themes.validate(write(self.root, "t", self.meta(), None))


class Discover(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.bundled = Path(temp.name) / "bundled"
        self.user = Path(temp.name) / "user"
        self.bundled.mkdir()
        self.user.mkdir()
        for name, value in (("BUNDLED", self.bundled), ("USER", self.user)):
            patcher = mock.patch.object(themes, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_finds_themes_sorted_by_name(self):
        write(self.bundled, "zed")
        write(self.bundled, "alpha")
        found, problems = themes.discover()
        self.assertEqual([t["id"] for t in found], ["alpha", "zed"])
        self.assertEqual(problems, [])

    def test_user_theme_overrides_a_bundled_one(self):
        write(self.bundled, "orb")
        write(self.user, "orb")
        found, _ = themes.discover()
        self.assertEqual([t["path"] for t in found], [str(self.user / "orb")])

    def test_a_broken_theme_is_a_problem_not_a_crash(self):
        write(self.user, "good")
        for name, meta in (("broken-json", "{"), ("a-list", "[]"), ("a-number", "5")):
            write(self.user, name, meta)
        write(self.user, "no-shader", None, None)
        found, problems = themes.discover()
        self.assertEqual([t["id"] for t in found], ["good"])
        self.assertEqual(len(problems), 4)
        self.assertTrue(all(p["path"] and p["error"] for p in problems))

    def test_missing_user_folder(self):
        self.user.rmdir()
        self.assertEqual(themes.discover(), ([], []))


def nested_macros(depth: int) -> str:
    """Under 350 bytes, and glCompileShader recurses until the stack is gone."""
    names = string.ascii_uppercase
    lines = ["#define A x+x"] + [f"#define {names[i]} {names[i - 1]}+{names[i - 1]}"
                                 for i in range(1, depth)]
    return "\n".join(lines) + f"\nvec4 tux_theme(vec2 uv){{float x=uv.x;return vec4({names[depth - 1]});}}\n"


def flat_sum(size: int) -> str:
    """The same crash with nothing in it that a rule could object to."""
    head, tail = "vec4 tux_theme(vec2 uv){float a=uv.x;return vec4(", "a);}\n"
    return head + "a+" * ((size - len(head) - len(tail)) // 2) + tail


# Each level is allowed, and so is calling the next one. It takes the draw to find out.
SLOW = "float f0(float x){return sin(x);}\n" + "".join(
    f"float f{n}(float x){{float s=x;for(int i=0;i<1000;i++){{s+=f{n - 1}(s+float(i));}}return s;}}\n"
    for n in range(1, 5)) + "vec4 tux_theme(vec2 uv){return vec4(f4(uv.x));}\n"


class Hostile(unittest.TestCase):
    """What a shared theme can hold. Everything here must end up a problem, quickly."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.user = Path(temp.name) / "user"
        self.user.mkdir()
        write(self.user, "good")
        patcher = mock.patch.object(themes, "USER", self.user)
        patcher.start()
        self.addCleanup(patcher.stop)

    def meta(self, **changes):
        return {"id": "bad", "name": "Bad", "author": "Me", "version": "1.0.0", "layout": ORB,
                **changes}

    def discover(self, timeout=5):
        done = []
        thread = threading.Thread(target=lambda: done.append(themes.discover()), daemon=True)
        thread.start()
        thread.join(timeout)
        self.assertTrue(done, "discover() hung")
        return done[0]

    def assert_only_bad_is_lost(self, error="."):
        found, problems = self.discover()
        ids = [t["id"] for t in found]
        self.assertIn("good", ids)
        self.assertIn("orb", ids, "a bundled theme went missing")
        self.assertNotIn("bad", ids)
        self.assertEqual(len(problems), 1, problems)
        self.assertRegex(problems[0]["error"], error)
        json.dumps(problems).encode()

    def test_fifos(self):
        for name in ("theme.json", "shader.glsl"):
            with self.subTest(name):
                folder = write(self.user, "bad")
                (folder / name).unlink()
                os.mkfifo(folder / name)
                self.assert_only_bad_is_lost("regular file")
                (folder / name).unlink()
                (folder / "theme.json").unlink(missing_ok=True)
                (folder / "shader.glsl").unlink(missing_ok=True)
                folder.rmdir()

    def test_links_to_devices(self):
        folder = write(self.user, "bad")
        (folder / "shader.glsl").unlink()
        (folder / "shader.glsl").symlink_to("/dev/zero")
        self.assert_only_bad_is_lost("regular file")

    def test_a_link_to_a_regular_file_is_fine(self):
        folder = write(self.user, "linked")
        target = folder.parent.parent / "shader.glsl"
        target.write_text(SHADER)
        (folder / "shader.glsl").unlink()
        (folder / "shader.glsl").symlink_to(target)
        self.assertEqual(self.discover()[1], [])

    def test_huge_sparse_files(self):
        for name in ("theme.json", "shader.glsl"):
            with self.subTest(name):
                folder = write(self.user, "bad")
                os.truncate(folder / name, 2 << 30)
                self.assert_only_bad_is_lost("too large")
                (folder / "theme.json").unlink()
                (folder / "shader.glsl").unlink()
                folder.rmdir()

    def test_json_python_takes_but_nothing_else_should(self):
        good = json.dumps(self.meta())
        for name, text in {
            "deep": "[" * 8000 + "]" * 8000,
            "NaN size": good.replace('"size": 220', '"size": NaN'),
            "Infinity size": good.replace('"size": 220', '"size": Infinity'),
            "huge size": good.replace('"size": 220', '"size": 1e999'),
            "NaN margin": good.replace('"margin": 32', '"margin": NaN'),
            "surrogate": good.replace('"Bad"', r'"\ud800"'),
            "long name": good.replace('"Bad"', json.dumps("x" * 101)),
        }.items():
            with self.subTest(name):
                folder = write(self.user, "bad", text)
                self.assert_only_bad_is_lost()
                (folder / "theme.json").unlink()
                (folder / "shader.glsl").unlink()
                folder.rmdir()

    def test_any_exception_stays_with_its_theme(self):
        for error in (RecursionError("deep"), OverflowError("big"), MemoryError(), RuntimeError()):
            real = themes.validate

            def flaky(folder, *args, error=error):
                if folder.name == "bad":
                    raise error
                return real(folder, *args)

            folder = write(self.user, "bad")
            with self.subTest(error), mock.patch.object(themes, "validate", flaky):
                self.assert_only_bad_is_lost()
            (folder / "theme.json").unlink()
            (folder / "shader.glsl").unlink()
            folder.rmdir()

    def test_bad_numbers(self):
        cases = {"margin": ("x", "40", [1, 2], {}, None, True, 1e308, 5000, -300),
                 "size": ("220", "\u0662\u0662\u0660", True, [220])}
        for key, values in cases.items():
            for value in values:
                with self.subTest(key=key, value=value), tempfile.TemporaryDirectory() as root:
                    with self.assertRaisesRegex(ValueError, f"layout.{key}"):
                        themes.validate(write(Path(root), "t", {
                            **self.meta(id="t"), "layout": {**ORB, key: value}}), tested=False)

    def test_margin_is_optional(self):
        layout = {k: v for k, v in ORB.items() if k != "margin"}
        meta = themes.validate(write(self.user, "t", {**self.meta(id="t"), "layout": layout}))
        self.assertEqual(meta["layout"]["margin"], 32)

    def test_height_is_optional_and_makes_a_square_if_left_out(self):
        meta = themes.validate(write(self.user, "t", self.meta(id="t")))
        self.assertEqual(meta["layout"]["height"], ORB["size"])
        wide = themes.validate(write(self.user, "w", {
            **self.meta(id="w"), "layout": {**ORB, "height": 70}}))
        self.assertEqual(wide["layout"]["height"], 70)

    def test_only_known_fields_leave(self):
        meta = themes.validate(write(self.user, "t", {
            **self.meta(id="t"), "extra": "x" * 1000, "layout": {**ORB, "extra": 1}}))
        self.assertEqual(set(meta), {"id", "name", "author", "version", "layout", "path"})
        self.assertEqual(set(meta["layout"]), {"kind", "size", "height", "anchor", "margin"})

    def test_a_folder_name_that_is_not_text(self):
        os.mkdir(os.fsencode(self.user) + b"/bad-\xff")
        _, problems = self.discover()
        self.assertEqual(len(problems), 1)
        problems[0]["path"].encode()

    def test_many_unclosed_comments_are_quick(self):
        write(self.user, "bad", shader="/* " * 21000)
        started = time.monotonic()
        self.assert_only_bad_is_lost("tux_theme")
        self.assertLess(time.monotonic() - started, 1)

    def test_shader_rules(self):
        body = "vec4 tux_theme(vec2 uv) { %s return vec4(0.0); }\n"
        cases = {
            "#define T texture2D\n" + SHADER: "define",
            "# /* x */ define T 1\n" + SHADER: "define",
            "#undef X\n" + SHADER: "undef",
            "#pragma x\n" + SHADER: "pragma",
            "#extension GL_ARB_gpu_shader5 : enable\n" + SHADER: "extension",
            "#line 1\n" + SHADER: "line",
            "#ver\\\nsion 330\n" + SHADER: "backslash",
            SHADER + "vec4 f(vec2 p) { return tex\\\nture2D(s, p); }": "backslash",
            "/* *\\\n/ #version 330\n" + SHADER: "backslash",
            body % "while (true) { }": "while",
            body % "do { } while (true);": "do",
            body % "for (;;) { }": "for \\(int",
            body % "for (int i = 0; i < 4; i++) ;": "for \\(int",
            body % "for (int i = 0; i < 4; i += 2) { }": "for \\(int",
            body % "for (int i = 0; i < n; i++) { }": "for \\(int",
            body % "for (float i = 0.0; i < 1.0; i += 0.001) { }": "for \\(int",
            body % "for (int i = 0; i != 4; i++) { }": "for \\(int",
            body % "for (int i = 0; i < 4; i--) { }": "never reaches",
            body % "for (int i = 9; i > 4; i++) { }": "never reaches",
            body % "for (int i = 0; i < 5000; i++) { }": "per pixel",
            body % "for (int i = 0; i < 40; i++) { for (int j = 0; j < 40; j++) { } }": "per pixel",
            body % "for (int i = 0; i < 4; i++) { i = 0; }": "counter",
            body % "for (int i = 0; i < 4; i++) { i--; }": "counter",
            body % "for (int i = 0; i < 4; i++) { --i; }": "counter",
            body % "for (int i = 0; i < 4; i++) { i *= 2; }": "counter",
        }
        for shader, error in cases.items():
            with self.subTest(shader), self.assertRaisesRegex(ValueError, error):
                themes.check_shader(shader)

    def test_loops_the_rules_allow(self):
        body = "vec4 tux_theme(vec2 uv) { float s = 0.0; %s return vec4(s); }\n"
        for loop in ("for (int i = 0; i < 4; i++) { s += 1.0; }",
                     "for (int i = 10; i >= 1; --i) { if (i == 3) { s = 0.0; } }",
                     "for(int i=0;i<30;i++){for(int j=0;j<30;j++){s+=float(i+j);}}"):
            with self.subTest(loop):
                themes.check_shader(body % loop)


@unittest.skipUnless(ctypes.util.find_library("EGL"), "needs EGL")
class Trial(unittest.TestCase):
    """The real thing, on software rendering in a process of its own."""

    def setUp(self):
        themes._trials.clear()

    def test_a_good_shader(self):
        self.assertIsNone(themes.trial(SHADER.encode()))

    def test_macros_that_crash_the_compiler(self):
        self.assertRegex(themes.trial(nested_macros(20).encode()), "crashes")

    def test_a_flat_expression_that_crashes_the_compiler(self):
        source = flat_sum(60000)
        themes.check_shader(source)
        self.assertRegex(themes.trial(source.encode()), "crashes")

    def test_a_shader_that_never_finishes(self):
        themes.check_shader(SLOW)
        self.assertRegex(themes.trial(SLOW.encode()), "too slow")

    def test_a_glsl_mistake_is_named_with_its_line(self):
        problem = themes.trial(b"vec4 tux_theme(vec2 uv) {\n  return nope;\n}\n")
        self.assertRegex(problem, "does not compile: 1:1.*nope")

    def test_every_bundled_shader_passes_with_room_to_spare(self):
        for folder in themes.BUNDLED.iterdir():
            with self.subTest(folder.name):
                self.assertIsNone(themes.trial((folder / "shader.glsl").read_bytes()))

    def test_answers_are_kept(self):
        with mock.patch.object(themes, "_run_trial", return_value=(None, True)) as run:
            themes.trial(b"x")
            themes.trial(b"x")
        run.assert_called_once()

    def test_no_answer_is_not_kept_and_keeps_the_theme_out(self):
        with mock.patch.object(themes, "_run_trial", return_value=("no answer", False)) as run:
            self.assertEqual(themes.trial(b"x"), "no answer")
            self.assertEqual(themes.trial(b"x"), "no answer")
        self.assertEqual(run.call_count, 2)

    def test_without_opengl_user_themes_stay_out(self):
        with tempfile.TemporaryDirectory() as folder:
            script = Path(folder, "trial.py")
            script.write_text(f"import sys\nsys.exit({themes.NO_GL})\n")
            with mock.patch.object(themes, "TRIAL", script):
                self.assertRegex(themes.trial(b"x"), "could not test")

    def test_the_budget_stops_trials_but_not_the_listing(self):
        with tempfile.TemporaryDirectory() as root, \
                mock.patch.object(themes, "USER", Path(root)), \
                mock.patch.object(themes, "TRIAL_BUDGET_S", -1):
            write(Path(root), "late")
            found, problems = themes.discover()
        self.assertIn("orb", [t["id"] for t in found])
        self.assertRegex(problems[0]["error"], "not tested yet")

    def test_bundled_themes_are_not_tried(self):
        with mock.patch.object(themes, "trial", side_effect=AssertionError):
            self.assertTrue(themes.discover()[0])

    def test_a_crash_leaves_no_core_and_touches_no_cache(self):
        with tempfile.TemporaryDirectory() as folder:
            before = set(os.listdir(folder))
            with mock.patch.dict(themes.TRIAL_ENV, {"HOME": folder, "XDG_CACHE_HOME": folder}):
                themes.trial(nested_macros(20).encode())
            self.assertEqual(set(os.listdir(folder)), before)


class Bundled(unittest.TestCase):
    def test_every_bundled_theme_passes(self):
        folders = [p for p in themes.BUNDLED.iterdir() if (p / "theme.json").exists()]
        for folder in folders:
            with self.subTest(folder.name):
                themes.validate(folder, tested=False)


if __name__ == "__main__":
    unittest.main()
