"""Theme discovery. A theme is data only: theme.json + a GLSL fragment shader.

Shaders run on the GPU with no file or network access, but inside the compositor: one that crashes
the GLSL compiler or never finishes takes the whole session down. So nothing is taken on trust
here. The files are read as if they were hostile, the source has to follow rules that keep it
easy to reason about, and a user theme is only offered once a throwaway process has compiled,
linked and drawn it. None of that can prove a shader harmless; see docs/THEMES.md.
"""
import hashlib
import json
import math
import os
import re
import signal
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

BUNDLED = Path(__file__).resolve().parent.parent / "themes"
USER = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "tuxistant/themes"
MAX_SHADER_BYTES = 64 * 1024
MAX_META_BYTES = 16 * 1024
MAX_TEXT_CHARS = 100
MAX_MARGIN = 256
MAX_LOOP_RUNS = 1024    # per pixel, nested loops multiplied
ANCHORS = {"bottom-right", "bottom-left", "top-right", "top-left", "bottom-center", "center"}
# \Z so an unclosed comment ends the matching in one go instead of once for every "/*" in it.
COMMENTS = re.compile(r"/\*.*?(?:\*/|\Z)|//[^\n]*", re.S)
DIRECTIVE = re.compile(r"#\s*(\w*)")
DIRECTIVES = {"if", "ifdef", "ifndef", "elif", "else", "endif"}
FORBIDDEN = re.compile(r"\btexture\w*\s*\(|\bsampler\w+|\b(?:while|do)\b")
LOOP = re.compile(
    r"for\s*\(\s*int\s+(?P<i>\w+)\s*=\s*(?P<start>-?\d+)\s*;"
    r"\s*(?P=i)\s*(?P<test>[<>]=?)\s*(?P<stop>-?\d+)\s*;"
    r"\s*(?:(?P=i)\s*(?P<step>\+\+|--)|(?P<pre>\+\+|--)\s*(?P=i))\s*\)\s*\{")

TRIAL = Path(__file__).with_name("shader_trial.py")
TRIAL_TIMEOUT_S = 10    # the trial limits its CPU time. This is for when it never gets any
TRIAL_BUDGET_S = 12     # per listing, so a folder of slow shaders cannot use up the caller's wait
GLSL_ERROR, NO_GL = 10, 11
# llvmpipe, so a bad shader never gets near the GPU, and the same compiler front end the shell has.
# Without the cache Mesa skips compiling what it has seen.
TRIAL_ENV = {"EGL_PLATFORM": "surfaceless", "LIBGL_ALWAYS_SOFTWARE": "1",
             "GALLIUM_DRIVER": "llvmpipe", "LP_NUM_THREADS": "0", "mesa_glthread": "false",
             "MESA_SHADER_CACHE_DISABLE": "true"}
_trials = {}
_trials_lock = threading.Lock()


def _read(path: Path, limit: int) -> bytes:
    """A shared theme can hold a FIFO, a link to a device or a huge sparse file."""
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOCTTY | os.O_CLOEXEC)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError(f"{path.name} is not a regular file")
        data = os.read(fd, limit + 1)
    finally:
        os.close(fd)
    if len(data) > limit:
        raise ValueError(f"{path.name} too large")
    return data


def _no_constant(name: str):
    raise ValueError(f"theme.json may not use {name}")


def _number(layout: dict, key: str, low: float, high: float, default=None):
    """int() and float() would take strings, such as digits from other scripts."""
    value = layout.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high:
        raise ValueError(f"layout.{key} must be a number, {low}..{high}")
    return value


def _layout(layout) -> dict:
    if not isinstance(layout, dict) or layout.get("kind") not in ("orb", "band"):
        raise ValueError("layout.kind must be orb or band")
    if layout["kind"] == "band":
        return {"kind": "band", "height": _number(layout, "height", 0.05, 1.0, 0)}
    if layout.get("anchor") not in ANCHORS:
        raise ValueError(f"layout.anchor must be one of {sorted(ANCHORS)}")
    size = _number(layout, "size", 64, 1024, 0)
    return {"kind": "orb", "size": size, "height": _number(layout, "height", 32, 1024, size),
            "anchor": layout["anchor"], "margin": _number(layout, "margin", 0, MAX_MARGIN, 32)}


def _check_loops(source: str):
    """Keeps the work per pixel countable, and a hang out of reach of branches the trial misses."""
    depth, loops = 0, []    # the loops around this point: (body depth, runs, counter, body start)
    for token in re.finditer(r"\bfor\b|[{}]", source):
        if token[0] == "{":
            depth += 1
        elif token[0] == "}":
            depth -= 1
            while loops and loops[-1][0] > depth:
                _, _, counter, start = loops.pop()
                assigned = re.compile(rf"\b{counter}\s*(?:[-+*/%&|^]?=(?!=)|<<=|>>=|\+\+|--)"
                                      rf"|(?:\+\+|--)\s*{counter}\b")
                if assigned.search(source, start, token.start()):
                    raise ValueError(f"a loop may not change its counter {counter}")
        else:
            loop = LOOP.match(source, token.start())
            if not loop:
                raise ValueError("loops must look like for (int i = 0; i < 8; i++) {")
            if (loop["step"] or loop["pre"]).startswith("+") != loop["test"].startswith("<"):
                raise ValueError(f"the loop on {loop['i']} never reaches its end")
            loops.append((depth + 1, abs(int(loop["stop"]) - int(loop["start"])) + 1, loop["i"],
                          loop.end()))
            if math.prod(entry[1] for entry in loops) > MAX_LOOP_RUNS:
                raise ValueError(f"loops may run at most {MAX_LOOP_RUNS} times per pixel")


def check_shader(text: str):
    """The rules in docs/THEMES.md that can be read off the source."""
    if "\\" in text:    # a line continuation could spell out anything the checks below look for
        raise ValueError("shader may not use a backslash")
    source = COMMENTS.sub(" ", text)
    if not re.search(r"\bvec4\s+tux_theme\s*\(", source):
        raise ValueError("shader must define vec4 tux_theme(vec2 uv)")
    for directive in DIRECTIVE.finditer(source):
        if directive[1] not in DIRECTIVES:
            raise ValueError(f"shader may not use #{directive[1]}")
    if found := FORBIDDEN.search(source):
        raise ValueError(f"shader may not use {found[0].strip()}")
    _check_loops(source)


def _run_trial(source: bytes) -> tuple[str | None, bool]:
    """(why the shader must not reach the shell or None, whether that answer can be kept)."""
    try:
        done = subprocess.run([sys.executable, "-I", "-S", str(TRIAL)], input=source,
                              capture_output=True, env=TRIAL_ENV, timeout=TRIAL_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return "shader test timed out", False
    except OSError as e:
        return f"could not test the shader: {e.strerror}", False
    match done.returncode:
        case 0:
            return None, True
        case code if code == GLSL_ERROR:
            return "shader does not compile: " + done.stdout.decode("utf-8", "replace"), True
        case code if code == NO_GL:
            return "could not test the shader: no software OpenGL here", False
        case code if code in (-signal.SIGXCPU, -signal.SIGKILL):
            return "shader is too slow", True
        case _:
            return "shader crashes the GLSL compiler", True


def trial(source: bytes, deadline: float = math.inf) -> str | None:
    """Why a shader must not reach the shell, or None. No answer counts as a no: a theme that
    could not be tested stays out, because being wrong about it costs the session."""
    digest = hashlib.sha256(source).hexdigest()
    with _trials_lock:
        if digest in _trials:
            return _trials[digest]
        if time.monotonic() > deadline:
            return "shader not tested yet, try again"
        problem, final = _run_trial(source)
        if final:
            _trials[digest] = problem
        return problem


def validate(folder: Path, tested: bool = True, deadline: float = math.inf) -> dict:
    """The theme's fields, or an exception. tested=False skips the trial, for bundled themes."""
    meta = json.loads(_read(folder / "theme.json", MAX_META_BYTES).decode(),
                      parse_constant=_no_constant)
    if not isinstance(meta, dict):
        raise ValueError("theme.json must be an object")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,40}", str(meta.get("id", ""))):
        raise ValueError("id must be lowercase letters, digits and dashes")
    if meta["id"] != folder.name:
        raise ValueError("id must match the folder name")
    for key in ("name", "author", "version"):
        if not isinstance(meta.get(key), str) or not meta[key]:
            raise ValueError(f"missing {key}")
        if len(meta[key]) > MAX_TEXT_CHARS:
            raise ValueError(f"{key} is longer than {MAX_TEXT_CHARS} characters")
        meta[key].encode()      # a lone surrogate passes json but not GTK
    layout = _layout(meta.get("layout"))
    source = _read(folder / "shader.glsl", MAX_SHADER_BYTES)
    check_shader(source.decode())
    if tested and (problem := trial(source, deadline)):
        raise ValueError(problem)
    return {**{key: meta[key] for key in ("id", "name", "author", "version")},
            "layout": layout, "path": str(folder)}


def _problem(folder: Path, error: Exception) -> dict:
    """Both end up in GTK labels, and neither has to be valid text."""
    def readable(text): return text.encode("utf-8", "replace").decode()[:300]
    return {"path": readable(str(folder)), "error": readable(str(error) or type(error).__name__)}


def discover() -> tuple[list[dict], list[dict]]:
    """Returns (themes, problems). User themes override bundled ones with the same id."""
    found, problems = {}, []
    deadline = time.monotonic() + TRIAL_BUDGET_S
    for root in (BUNDLED, USER):
        try:
            folders = sorted(p for p in root.iterdir() if p.is_dir())
        except OSError:
            continue
        for folder in folders:
            try:
                meta = validate(folder, root != BUNDLED, deadline)
                found[meta["id"]] = meta
            except Exception as e:      # one bad theme must never cost the others
                problems.append(_problem(folder, e))
    return sorted(found.values(), key=lambda t: t["name"]), problems
