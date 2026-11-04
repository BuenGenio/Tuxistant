// Scanner: the lamp bar from an 80s talking car. One row of lamps sweeps back and forth in a black
// bezel, and the voice lights it from the middle outward.
//
// Space is y-up in design pixels: origin at the centre, 300 by 70 across the actor. At the theme's
// own size every lamp edge below lands on a pixel boundary, so the lamps stay crisp.
//
// u_time is only ever scaled by constants: a speed taken from a state would jump the phase by
// u_time * delta on every change. So "sweeps faster" is a second, faster scanner faded in.

const vec2 SC_REF = vec2(300.0, 70.0);
const float SC_TAU = 6.2831853;
const vec2 SC_PANEL = vec2(147.0, 32.0);       // half extents, as are all the vec2 sizes here
const float SC_PANEL_R = 20.0;
const vec2 SC_LIGHT = vec2(-0.35, 0.94);       // towards the light: above, a little to the left
const vec2 SC_WELL = vec2(132.0, 17.0);        // the recessed window around the lamps
const float SC_STEPS = 16.0;                   // spaces between lamps, so 17 lamps
const float SC_SPAN = 120.0;                   // distance from the middle to an end lamp
const vec2 SC_LAMP = vec2(6.0, 10.0);
const float SC_BLEED = 27.0;                   // how far light spills past what is lit

float sc_box(vec2 p, vec2 b, float r) {        // rounded box, signed distance
    vec2 q = abs(p) - b + r;
    return length(max(q, 0.0)) + min(max(q.x, q.y), 0.0) - r;
}

float sc_well(vec2 p) {
    return sc_box(p, SC_WELL, 7.0);
}

float sc_cov(float d, float ppd) {             // one pixel edge, exactly 0 outside the shape
    return clamp(0.5 - d * ppd, 0.0, 1.0);
}

float sc_halo(vec2 p, vec2 b) {                // light around a box, 0 beyond SC_BLEED
    float f = max(1.0 - length(max(abs(p) - b, 0.0)) / SC_BLEED, 0.0);
    float f2 = f * f;
    return f2 * (0.5 + 0.5 * f2 * f2);         // a tight core on a long, faint tail
}

// A head easing over x = 0..1 and back once per cycle, x = (1 - cos(tau * phase)) / 2. It passes
// x at phase a going right and 1 - a going left, so the time since the later pass gives the trail
// with no history to keep, and the slow turn at each end leaves the end lamps burning longest.
float sc_sweep(float x, float phase, float tail) {
    float a = acos(1.0 - 2.0 * x) / SC_TAU;
    float age = min(fract(phase - a), fract(phase + a));
    float fade = max(1.0 - age / tail, 0.0);
    float head = 0.5 - 0.5 * cos(SC_TAU * phase);
    // Flat on top, so the head stays at full power between two lamps instead of pulsing as it
    // hops, then falling off so that lamps warm up just ahead of it.
    float rise = clamp(1.35 - 12.0 * abs(x - head), 0.0, 1.0);
    return max(0.7 * fade * fade, rise * rise);
}

float sc_scan(float x) {
    float slow = sc_sweep(x, fract(u_time * 0.5), 0.20);
    float fast = sc_sweep(x, fract(u_time * 1.4), 0.24);
    return mix(slow, fast, u_think);
}

// How lit the row is at x, which is 0 and 1 at the end lamps. The voice fills it from the middle,
// over the top of the sweep.
float sc_lit(float x, float sweep, float voice) {
    float meter = clamp((voice - abs(2.0 * x - 1.0)) * 6.0, 0.0, 1.0);
    return max(sweep * sc_scan(x), meter);
}

// q from the lamp's centre, on 0..1. hot is how far full power burns past red towards white.
vec3 sc_led(vec2 q, vec2 b, float on, float hot) {
    vec2 e = q / b;
    float core = max(1.0 - 0.55 * dot(e, e), 0.0);
    vec3 lit = mix(vec3(0.68, 0.012, 0.008), vec3(1.0, 0.075, 0.025), core);
    vec3 burn = vec3(0.0, 0.56, 0.24) * (hot * on * on * on * core * core);
    vec3 dark = vec3(0.060, 0.007, 0.006) + 0.014 * max(e.y, 0.0);   // unlit, lens catching the light
    return mix(dark, lit, on) + burn;
}

vec4 tux_theme(vec2 uv) {
    float ppd = max(min(u_size.x / SC_REF.x, u_size.y / SC_REF.y), 0.001);   // pixels per design pixel
    vec2 p = (uv - 0.5) * u_size / ppd;
    p *= vec2(1.0, -1.0) * (1.0 + 0.04 * (1.0 - u_appear));   // y-up, growing into place

    float edge = sc_box(p, SC_PANEL, SC_PANEL_R);
    float alpha = sc_cov(edge, ppd);
    if (alpha <= 0.0)
        return vec4(0.0);

    // Body: black plastic lit from above, with a moulded bevel and a fine bright rim. slope is
    // +2 where the surface faces the light and -2 where it faces away.
    float top = 0.5 + 0.5 * p.y / SC_PANEL.y;
    vec3 col = vec3(0.016, 0.016, 0.019) + vec3(0.034, 0.034, 0.039) * top * top;
    float slope = sc_box(p + SC_LIGHT, SC_PANEL, SC_PANEL_R) - sc_box(p - SC_LIGHT, SC_PANEL, SC_PANEL_R);
    float bevel = clamp(1.0 + edge / 7.0, 0.0, 1.0);
    col += 0.075 * bevel * bevel * max(slope, 0.0);
    col *= 1.0 - 0.3 * bevel * max(-slope, 0.0);
    col += (0.03 + 0.085 * max(slope, 0.0)) * clamp(1.0 + edge * ppd * 0.7, 0.0, 1.0);

    // A smoked window set into the panel. Being recessed, its lower lip catches the light.
    float well = sc_well(p);
    float lip = sc_well(p + SC_LIGHT) - sc_well(p - SC_LIGHT);
    col = mix(col, vec3(0.0055, 0.0045, 0.0055), sc_cov(well, ppd));
    col += (0.012 + 0.05 * max(-lip, 0.0)) * clamp(1.0 - abs(well) * ppd * 0.6, 0.0, 1.0);

    // The sweep leads while thinking and dims under the voice, which the row shows while listening
    // and, harder, while speaking.
    float power = smoothstep(0.35, 1.0, u_appear);
    float sweep = 0.90 + 0.10 * u_think - 0.30 * u_speak;
    float voice = min(1.15 * u_level * (0.76 * u_listen + u_speak), 1.0);

    // Each lamp takes the row's value at its own centre.
    float x = 0.5 + 0.5 * p.x / SC_SPAN;       // 0 and 1 at the end lamps
    float lamp = clamp(floor(x * SC_STEPS + 0.5), 0.0, SC_STEPS);
    vec2 l = vec2(p.x - SC_SPAN * (2.0 * lamp / SC_STEPS - 1.0), p.y);
    float bulb = power * sc_lit(lamp / SC_STEPS, sweep, voice);
    col = mix(col, sc_led(l, SC_LAMP, bulb, 0.9), sc_cov(sc_box(l, SC_LAMP, 2.5), ppd));

    // Warm glow, from the row's value at this exact x.
    float glow = (0.9 + 0.5 * u_think) * sc_lit(clamp(x, 0.0, 1.0), sweep, voice)
               * sc_halo(p, vec2(SC_SPAN, SC_LAMP.y));
    col += mix(vec3(0.26, 0.012, 0.004), vec3(0.40, 0.055, 0.012), min(glow, 1.0)) * (glow * power);

    // A slight sheen: one soft diagonal reflection, crisper on its upper edge.
    float s = 0.5 * p.x + p.y;
    col += 0.03 * smoothstep(-70.0, 24.0, s) * (1.0 - smoothstep(18.0, 54.0, s));

    // A dark gradient has only a few 8-bit steps to use: half a step of noise hides them.
    float noise = fract(52.9829189 * fract(dot(gl_FragCoord.xy, vec2(0.06711056, 0.00583715))));
    col += (noise - 0.5) / 255.0;

    return vec4(clamp(col, 0.0, 1.0) * alpha, alpha);
}
