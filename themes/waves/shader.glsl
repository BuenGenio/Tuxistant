// Waves: long ribbons of light across the bottom of the screen, after the flowing lines of
// classic handheld console menus. Each line is a bright crest over a soft sheet of light.
//
// Every line rides one shared carrier and adds its own drift, so the set reads as a single ribbon
// that gathers and fans out rather than a tangle of unrelated curves.
//
// u_time is only ever scaled by constants. A speed taken from a state or from u_level would
// jump the phase by u_time * delta on every change, so states and level change the size of
// fixed-speed oscillators instead.

// 1 at x = 0 and exactly 0 from |x| >= 1, so nothing is drawn beyond a line's reach.
float wv_bump(float x) {
    float q = max(1.0 - x * x, 0.0);
    return q * q;
}

// Interleaved gradient noise, 0..1.
float wv_grain(vec2 p) {
    return fract(52.9829189 * fract(dot(p, vec2(0.06711056, 0.00583715))));
}

vec4 tux_theme(vec2 uv) {
    float height = max(u_size.y, 1.0);
    float xc = (uv.x - 0.5) * u_size.x / height;  // band heights from centre: same curves at any width
    float nx = 2.0 * uv.x - 1.0;
    float centre = 1.0 - 0.55 * nx * nx;          // the voice is strongest mid-screen

    float voice = u_level * min(u_listen + u_speak, 1.0);

    // Speaking: a beat runs outward from the centre, its strength set by the level.
    float beat = 0.5 + 0.5 * sin(u_time * 7.0 - length(vec2(xc, 0.5)) * 2.2);
    float pulse = mix(1.0, 0.5 + beat, u_speak);

    // Thinking: the ribbon goes calm and one slow hump travels along it, lifting and lighting it.
    float swell = 0.5 + 0.5 * sin(xc * 1.3 - u_time * 0.7);
    swell *= swell * swell * u_think;
    float calm = 1.0 - 0.6 * u_think;

    float drive = (voice + 0.10 * u_speak) * pulse * centre;
    // The level has a 40 ms attack and a shader cannot filter it, so most of the voice goes to the
    // ripple: driving the long carrier as hard makes the whole ribbon lurch on every syllable.
    float amp = (0.05 + 0.012 * u_listen) * calm + 0.08 * drive;
    float ripple = 0.048 * drive * centre;
    float gain = 1.0 + 0.6 * voice * pulse + 0.6 * swell - 0.2 * u_think;
    float rise = (1.0 - u_appear) * 0.18;

    // The voice squeezes the curves towards the centre, so frequency rises there while the far
    // ends, where a small stretch would be a big phase jump, stay put.
    float xw = xc * (1.0 + 0.8 * voice / (1.0 + xc * xc));

    float carrier = sin(xw * 1.1 + u_time * 0.33) + 0.5 * sin(xw * 2.3 - u_time * 0.21 + 1.3);

    // One ripple runs through every line: in towards the middle while listening, as if drawn by
    // the voice, and out from it while speaking. Kept as sine and cosine parts so that each line
    // can shift its phase below with no per-pixel trig of its own.
    float wave = length(vec2(xw, 0.35)) * 8.0;
    float ripS = u_listen * sin(wave + u_time * 3.0) + u_speak * sin(wave - u_time * 3.4);
    float ripC = u_listen * cos(wave + u_time * 3.0) + u_speak * cos(wave - u_time * 3.4);

    float fan = (0.62 + 0.38 * sin(xc * 0.8 - u_time * 0.17 + 0.6)) * (calm + 0.4 * voice);

    float core = max(1.4 / height, 0.0034);       // crest half width, never under about a pixel
    float halo = core * (6.0 + 6.0 * voice);
    vec3 white = vec3(0.90, 0.95, 1.00);
    vec3 tint = vec3(0.40, 0.62, 0.92) + u_listen * vec3(-0.06, 0.06, 0.0)
              + u_think * vec3(0.08, -0.06, 0.0) + u_speak * vec3(0.14, 0.10, 0.0);

    // Back to front. Premultiplied "over" keeps rgb <= alpha and alpha <= 1 if each layer does.
    vec4 acc = vec4(0.0);
    for (int i = 0; i < 7; i++) {
        float fi = float(i);
        float f = fi / 6.0;                       // 0 at the back, 1 at the front
        float r = fract(fi * 0.618034 + 0.2);     // golden ratio: well spread per-line variety
        float ph = fi * 1.9;

        float own = sin(xw * (1.6 + 1.1 * r) + u_time * (0.28 + 0.30 * r) + ph);
        float rip = ripS * cos(f * 1.7) + ripC * sin(f * 1.7);
        float e = (f - 0.5) * 0.36 * fan
                + amp * (carrier * (0.7 + 0.5 * f) + 0.45 * own) + rip * ripple;
        e *= inversesqrt(1.0 + e * e * 6.9);      // limit of 0.38: lines stay on screen when loud
        float y = 0.55 + e - swell * (0.15 + 0.05 * f) + rise;

        // Light catches each ribbon in places, so a line is never one even wire.
        float shine = 0.5 + 0.5 * sin(xc * (0.9 + 0.5 * r) - u_time * (0.22 + 0.20 * r) + ph * 1.3);
        shine = mix(mix(0.35, 1.0, shine), 1.0, swell);

        float d = uv.y - y;                       // positive below the crest
        float depth = gain * mix(0.50, 1.0, f) * shine;
        float glow = 1.0 + (d / halo) * (d / halo);
        float sheet = exp(-max(d, 0.0) / (0.09 + 0.12 * r)) * smoothstep(-core, core, d);

        // A white centre with tinted flanks keeps the crest a crisp line on light wallpapers too.
        float crest = wv_bump(d / (core * 1.5));
        vec4 layer = vec4(mix(tint, white, crest * crest), 1.0) * (depth * 0.50 * crest)
                   + vec4(tint, 1.0) * (depth * (0.12 / (glow * glow) + 0.09 * sheet));
        layer /= max(layer.a, 1.0);               // a loud crest can pass 1
        acc = layer + acc * (1.0 - layer.a);
    }

    acc *= smoothstep(0.0, 0.30, uv.y);           // no hard edge at the top of the band

    // Low alpha spread over hundreds of pixels is only a few 8-bit steps: dither them away.
    float dither = (wv_grain(gl_FragCoord.xy) - 0.5) / 255.0 * min(acc.a * 255.0, 1.0);
    acc.rgb = clamp(acc.rgb + dither, 0.0, acc.a);
    return acc;
}
