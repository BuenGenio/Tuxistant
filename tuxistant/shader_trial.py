"""Compiles, links and draws one theme shader, to see what it would do to GNOME Shell.

A shader can crash or hang the GLSL compiler, and inside the shell that ends the session, so
themes.py runs this first in a process of its own. It reads the shader on stdin and exits 0 if it
is fine, GLSL_ERROR after printing what is wrong, or NO_GL. Anything else means the shader killed
it. Only the standard library, because it runs with -I -S.
"""
import ctypes
import resource
import sys

GLSL_ERROR, NO_GL = 10, 11
# Half of the 8 MiB main thread stack of the shell, so what passes here has room to spare there.
STACK_BYTES = 4 << 20
MEMORY_BYTES = 2 << 30
CPU_S = 6
SIZE = 64

HEADER = b"""#version 330 core
uniform float u_time;
uniform float u_level;
uniform float u_appear;
uniform float u_listen;
uniform float u_think;
uniform float u_speak;
uniform vec2 u_size;
in vec2 tux_uv;
out vec4 tux_color;
#line 0 1
"""
FOOTER = b"""
void main() {
    tux_color = tux_theme(tux_uv) * u_appear;
}
"""
VERTEX = b"""#version 330 core
out vec2 tux_uv;
void main() {
    vec2 corner = vec2(float((gl_VertexID << 1) & 2), float(gl_VertexID & 2));
    tux_uv = corner;
    gl_Position = vec4(corner * 2.0 - 1.0, 0.0, 1.0);
}
"""

EGL_NONE = 0x3038
EGL_OPENGL_API = 0x30A2
EGL_PLATFORM_SURFACELESS = 0x31DD
EGL_CONTEXT_MAJOR_VERSION, EGL_CONTEXT_MINOR_VERSION = 0x3098, 0x30FB
EGL_CONTEXT_PROFILE_MASK, EGL_CONTEXT_CORE_PROFILE = 0x30FD, 1
GL_FRAGMENT_SHADER, GL_VERTEX_SHADER = 0x8B30, 0x8B31
GL_COMPILE_STATUS, GL_LINK_STATUS = 0x8B81, 0x8B82
GL_TEXTURE_2D, GL_RGBA, GL_RGBA8, GL_UNSIGNED_BYTE = 0x0DE1, 0x1908, 0x8058, 0x1401
GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_TRIANGLES = 0x8D40, 0x8CE0, 0x0004

void_p, uint, integer, single = ctypes.c_void_p, ctypes.c_uint, ctypes.c_int, ctypes.c_float


class Gl:
    """Entry points by name, as the driver hands them out."""

    def __init__(self, egl):
        self.egl = egl
        egl.eglGetProcAddress.restype = void_p
        egl.eglGetProcAddress.argtypes = [ctypes.c_char_p]

    def bind(self, name, result, *arguments):
        address = self.egl.eglGetProcAddress(name.encode())
        if not address:
            sys.exit(NO_GL)
        return ctypes.CFUNCTYPE(result, *arguments)(address)


def context(gl):
    """A core context with no surface: the drawing goes to a framebuffer object."""
    display = gl.bind("eglGetPlatformDisplayEXT", void_p, integer, void_p, void_p)(
        EGL_PLATFORM_SURFACELESS, None, None)
    attributes = (integer * 7)(EGL_CONTEXT_MAJOR_VERSION, 3, EGL_CONTEXT_MINOR_VERSION, 3,
                               EGL_CONTEXT_PROFILE_MASK, EGL_CONTEXT_CORE_PROFILE, EGL_NONE)
    if not display or not gl.bind("eglInitialize", integer, void_p, void_p, void_p)(
            display, None, None):
        sys.exit(NO_GL)
    gl.bind("eglBindAPI", integer, uint)(EGL_OPENGL_API)
    created = gl.bind("eglCreateContext", void_p, void_p, void_p, void_p, void_p)(
        display, None, None, attributes)
    if not created or not gl.bind("eglMakeCurrent", integer, void_p, void_p, void_p, void_p)(
            display, None, None, created):
        sys.exit(NO_GL)


def compiled(gl, kind, source):
    shader = gl.bind("glCreateShader", uint, uint)(kind)
    text = ctypes.c_char_p(source)
    gl.bind("glShaderSource", None, uint, integer, void_p, void_p)(
        shader, 1, ctypes.addressof(text), None)
    gl.bind("glCompileShader", None, uint)(shader)
    passed = integer()
    gl.bind("glGetShaderiv", None, uint, uint, void_p)(
        shader, GL_COMPILE_STATUS, ctypes.addressof(passed))
    if not passed:
        fail(gl.bind("glGetShaderInfoLog", None, uint, integer, void_p, void_p), shader)
    return shader


def fail(read_log, handle):
    log = ctypes.create_string_buffer(400)
    read_log(handle, len(log), None, ctypes.addressof(log))
    print(log.value.decode("utf-8", "replace").strip())
    sys.exit(GLSL_ERROR)


def linked(gl, vertex, fragment):
    program = gl.bind("glCreateProgram", uint)()
    attach = gl.bind("glAttachShader", None, uint, uint)
    attach(program, vertex)
    attach(program, fragment)
    gl.bind("glLinkProgram", None, uint)(program)
    passed = integer()
    gl.bind("glGetProgramiv", None, uint, uint, void_p)(
        program, GL_LINK_STATUS, ctypes.addressof(passed))
    if not passed:
        fail(gl.bind("glGetProgramInfoLog", None, uint, integer, void_p, void_p), program)
    return program


def target(gl):
    texture, framebuffer, vertices = uint(), uint(), uint()
    gl.bind("glGenVertexArrays", None, integer, void_p)(1, ctypes.addressof(vertices))
    gl.bind("glBindVertexArray", None, uint)(vertices.value)
    gl.bind("glGenTextures", None, integer, void_p)(1, ctypes.addressof(texture))
    gl.bind("glBindTexture", None, uint, uint)(GL_TEXTURE_2D, texture.value)
    gl.bind("glTexImage2D", None, uint, integer, integer, integer, integer, integer, uint, uint,
            void_p)(GL_TEXTURE_2D, 0, GL_RGBA8, SIZE, SIZE, 0, GL_RGBA, GL_UNSIGNED_BYTE, None)
    gl.bind("glGenFramebuffers", None, integer, void_p)(1, ctypes.addressof(framebuffer))
    gl.bind("glBindFramebuffer", None, uint, uint)(GL_FRAMEBUFFER, framebuffer.value)
    gl.bind("glFramebufferTexture2D", None, uint, uint, uint, uint, integer)(
        GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_TEXTURE_2D, texture.value, 0)
    gl.bind("glViewport", None, integer, integer, integer, integer)(0, 0, SIZE, SIZE)


def draw(gl, program):
    """Once per state, as a loop the compiler could not see through only shows up here."""
    gl.bind("glUseProgram", None, uint)(program)
    location = gl.bind("glGetUniformLocation", integer, uint, ctypes.c_char_p)
    set_float = gl.bind("glUniform1f", None, integer, single)
    gl.bind("glUniform2f", None, integer, single, single)(
        location(program, b"u_size"), float(SIZE), float(SIZE))
    for name, value in ((b"u_time", 7.5), (b"u_level", 0.6), (b"u_appear", 1.0)):
        set_float(location(program, name), value)
    triangles = gl.bind("glDrawArrays", None, uint, integer, integer)
    finish = gl.bind("glFinish", None)
    for state in (b"u_listen", b"u_think", b"u_speak"):
        for name in (b"u_listen", b"u_think", b"u_speak"):
            set_float(location(program, name), 1.0 if name == state else 0.0)
        triangles(GL_TRIANGLES, 0, 3)
        finish()


def main():
    source = sys.stdin.buffer.read()
    for limit, value in ((resource.RLIMIT_CORE, 0), (resource.RLIMIT_STACK, STACK_BYTES),
                         (resource.RLIMIT_AS, MEMORY_BYTES)):
        resource.setrlimit(limit, (value, value))
    resource.setrlimit(resource.RLIMIT_CPU, (CPU_S, CPU_S + 1))
    try:
        gl = Gl(ctypes.CDLL("libEGL.so.1"))
    except OSError:
        sys.exit(NO_GL)
    context(gl)
    fragment = compiled(gl, GL_FRAGMENT_SHADER, HEADER + source + FOOTER)
    program = linked(gl, compiled(gl, GL_VERTEX_SHADER, VERTEX), fragment)
    target(gl)
    draw(gl, program)


if __name__ == "__main__":
    main()
