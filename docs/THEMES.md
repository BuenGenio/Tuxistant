# Writing a theme

A theme decides what Tux looks like on screen. It is a folder with two files:

```
~/.local/share/tuxistant/themes/pulse/
    theme.json     name, author and where it sits on screen
    shader.glsl    a GLSL function that draws it
```

Themes are data. There is nothing to install, and no code from a theme runs outside the shader. The shader does run inside GNOME Shell though, so see [what a theme can do](#what-a-theme-can-do) before installing one from someone you don't know.

## A complete example

Create the folder above, then save these two files.

`theme.json`:

```json
{
  "id": "pulse",
  "name": "Pulse",
  "author": "Your Name",
  "version": "1.0.0",
  "layout": { "kind": "orb", "size": 200, "anchor": "bottom-right", "margin": 32 }
}
```

`shader.glsl`:

```glsl
// A soft disc that swells with the voice and changes colour with the state.
vec4 tux_theme(vec2 uv) {
    vec2 p = (uv - 0.5) * 2.0;
    float radius = 0.55 + 0.25 * u_level + 0.03 * sin(u_time * 2.0);
    float disc = 1.0 - smoothstep(radius - 0.08, radius, length(p));

    vec3 color = vec3(0.35, 0.55, 1.00);
    color = mix(color, vec3(0.20, 0.85, 0.60), u_listen);
    color = mix(color, vec3(0.95, 0.75, 0.25), u_think);
    color = mix(color, vec3(0.95, 0.40, 0.55), u_speak);

    return vec4(color * disc, disc);    // premultiplied: colour is already scaled by alpha
}
```

Open Settings, go to Appearance, and press **Try** next to Pulse. The theme shows for about 4 seconds and cycles through listening, thinking and speaking. Choose it there, or under **Theme** in the panel icon's menu or the blob's right-click menu. New folders are found the next time the list is opened, so no restart is needed.

## theme.json

| Field | Rule |
|---|---|
| `id` | Lowercase letters, digits and dashes, starting with a letter or digit, at most 41 characters. Must equal the folder name |
| `name` | Shown in menus and Settings. At most 100 characters |
| `author` | Shown in Settings. At most 100 characters |
| `version` | Any non-empty string of at most 100 characters; `1.0.0` style is the convention |
| `layout` | One of the two forms below |

`orb` is a box floating over the desktop, `size` pixels wide:

| Field | Rule |
|---|---|
| `kind` | `"orb"` |
| `size` | A number, 64 to 1024 |
| `height` | A number, 32 to 1024; the same as `size` if left out, which makes a square |
| `anchor` | `bottom-right`, `bottom-left`, `top-right`, `top-left`, `bottom-center` or `center` |
| `margin` | A number of pixels from the screen edge, 0 to 256; 32 if left out |

`band` spans the full width of the monitor along its bottom edge and lets clicks through, so it has no right-click menu. The panel icon has the same **Theme** menu:

| Field | Rule |
|---|---|
| `kind` | `"band"` |
| `height` | A number: fraction of the monitor height, 0.05 to 1 |

## shader.glsl

Define one function:

```glsl
vec4 tux_theme(vec2 uv);
```

`uv` runs from 0 to 1 across the actor, with (0, 0) at the top-left. The result is premultiplied RGBA: the red, green and blue values must already be multiplied by alpha, or the edges glow.

These uniforms are already declared, so do not declare them again:

| Uniform | Type | Meaning |
|---|---|---|
| `u_time` | `float` | Seconds |
| `u_level` | `float` | Smoothed audio level, 0 to 1 |
| `u_appear` | `float` | Show and hide animation, 0 to 1. The host also multiplies your output by it, so you never need to fade yourself |
| `u_listen` | `float` | Blend towards listening, 0 to 1 |
| `u_think` | `float` | Blend towards thinking or connecting, 0 to 1 |
| `u_speak` | `float` | Blend towards speaking, 0 to 1 |
| `u_size` | `vec2` | Actor size in pixels. Use it to keep shapes round on a band |

The state blends move smoothly, so two of them can be between 0 and 1 at once. Mix towards each state as in the example rather than testing for one.

### Rules

- The only preprocessor directives are `#if`, `#ifdef`, `#ifndef`, `#elif`, `#else` and `#endif`. So no `#define`, `#version` or `#include`.
- No backslashes, not even in comments.
- No textures: no `texture...()` calls and no `sampler` types.
- No `while` and no `do`. Every loop has the form `for (int i = 0; i < 8; i++) {`: an `int` counter that counts by one between two literal numbers, with braces, and a body that leaves the counter alone.
- Nested loops multiplied together run at most 1024 times per pixel.
- Under 64 KB.

All of this is checked when themes are listed, and then a separate throwaway process compiles the shader, links it and draws a few frames with software rendering, with limits on its stack, memory and CPU time. A theme that breaks a rule, has a GLSL mistake, crashes that process or runs out of its time shows under **Themes with problems** in Settings, with the reason, and is not offered. If the test cannot run at all, for example because there is no software OpenGL, user themes stay out too. The bundled themes skip this test.

The first listing after a start tests each theme, which takes a moment. The results are kept until the daemon stops.

The test compiles for desktop OpenGL. GNOME Shell may still compile a shader differently, so always press **Try** and, if nothing draws, look in the log:

```sh
journalctl --user -b -g 'shader|tuxistant' /usr/bin/gnome-shell
```

The pattern is lower case on purpose. With a capital in it `journalctl` matches case-sensitively, and would miss `Shader compilation failed:`, the message that has the line numbers.

### Advice

- Multiply `u_time` by constants only. A speed taken from a state or from `u_level` jumps the animation by `u_time * change` every time the value moves. Let states change the size of a motion, not its rate.
- Keep the area outside your shape at exactly zero alpha, so nothing bleeds onto the desktop.
- Prefix helper functions with something of your own, such as `pl_`. Names starting with `tux_` and `cogl_` belong to the host, apart from `tux_theme`.
- The shader runs for every pixel on every frame inside the compositor. Keep loops short, above all on a band, which covers far more pixels than an orb.

## Sharing a theme

Pack the folder and send it:

```sh
cd ~/.local/share/tuxistant/themes && tar czf pulse.tar.gz pulse
```

To install one, unpack it into the themes folder. **Open themes folder** in Settings, under Appearance, takes you there. The folder name has to stay equal to `id`. A theme with the same `id` as a bundled one replaces it, which is also how to tweak a bundled theme: copy its folder from `themes/` in the checkout and edit the copy.

## What a theme can do

- A theme holds a JSON file that is parsed as data, and a shader that runs on the GPU. No Python or JavaScript from a theme is ever run.
- A shader can read only the uniforms above. It has no way to reach files, the network, the microphone, your history or your settings, and with textures banned it cannot sample anything from the screen.
- What it can do is hurt the desktop, because it is compiled and run inside GNOME Shell, and on Wayland a crash there ends your session. A few hundred bytes of GLSL are enough to crash the compiler, and a shader that never finishes freezes the screen. The rules and the test above exist to stop exactly that, and they stop every such shader we know of.

They cannot prove a shader harmless. The test uses software rendering, so a problem that only your GPU's own compiler has will not show, and it runs one set of values, so a slow branch that only opens later, say after an hour of `u_time`, can get past it. Only install themes from people you trust. If a theme misbehaves, delete its folder.

## Credits

The bundled themes are original work made for Tuxistant. The Tux-related artwork, in the Listening Tux theme, is drawn from scratch in GLSL out of mathematical shapes.
