// Listening Tux: the classic sitting penguin, after the Tux Larry Ewing drew with The GIMP in 1996,
// redrawn only with signed-distance shapes.
//
// Space is y-up and about -1..1 across the actor. Each shape is a distance in those units, and
// tx_cov turns a distance into a one pixel edge that is exactly 0 outside the shape, so nothing
// bleeds into the transparent background. Eyes and feet are mirrored with abs(x), so one
// evaluation draws both sides.
//
// u_time is only ever scaled by constants: a speed taken from a state or from u_level would jump
// the phase by u_time * delta on every change. States change the size of motions, not their rate.

vec2 tx_rot(vec2 p, float a) {            // the shape at p turns counter-clockwise by a
    float c = cos(a);
    float s = sin(a);
    return vec2(c * p.x + s * p.y, c * p.y - s * p.x);
}

float tx_ell(vec2 p, vec2 r) {            // ellipse, distance accurate near the edge
    float k0 = length(p / r);
    float k1 = length(p / (r * r));
    return k1 > 0.00001 ? k0 * (k0 - 1.0) / k1 : -min(r.x, r.y);
}

float tx_smin(float a, float b, float k) {
    float h = max(k - abs(a - b), 0.0) / k;
    return min(a, b) - h * h * k * 0.25;
}

float tx_cov(float d, float ppu) {        // ppu: pixels per unit
    return clamp(0.5 - d * ppu, 0.0, 1.0);
}

float tx_flipper(vec2 q, float a) {       // q from the shoulder, swung by a
    q = tx_rot(q, a);
    q.y += 0.26;
    return tx_ell(q, vec2(0.088 + 0.07 * q.y, 0.29));   // narrower toward the tip
}

// A flat foot turned outward: a broad pad with three toes along its front edge.
float tx_foot(vec2 q) {
    float toes = min(tx_ell(q - vec2(0.235, -0.03), vec2(0.115, 0.08)),
                 min(tx_ell(q - vec2(0.065, -0.065), vec2(0.115, 0.085)),
                     tx_ell(q - vec2(-0.105, -0.055), vec2(0.11, 0.08))));
    return tx_smin(tx_ell(q - vec2(0.04, 0.025), vec2(0.30, 0.115)), toes, 0.03);
}

vec4 tx_over(vec4 under, vec3 col, float a) {
    return vec4(col, 1.0) * a + under * (1.0 - a);
}

vec4 tux_theme(vec2 uv) {
    float side = max(min(u_size.x, u_size.y), 1.0);
    vec2 p = (uv - 0.5) * u_size * (2.0 / side);
    p.y = -p.y;

    // Pops up from the feet as it appears.
    vec2 foot = vec2(0.0, -0.90);
    float pop = mix(0.84, 1.0, u_appear);
    p = foot + (p - foot) / pop;

    // On a scaled monitor the shader runs per physical pixel, which u_size cannot tell us.
#if __VERSION__ >= 130
    float ppu = 1.0 / max(length(vec2(dFdx(p.x), dFdy(p.x))), 0.00001);
#else
    float ppu = 0.5 * side * pop;
#endif

    float talk = u_speak * min(u_level * 1.4, 1.0);
    float breath = sin(u_time * 1.7);

    // Pose. Sitting, so the feet and base stay planted, the body sways above them, and the head
    // pivots on the neck and trails the body a little, which is what turns the breathing into a bob.
    float lean = 0.015 * sin(u_time * 0.83) + 0.07 * u_listen - 0.02 * u_think;
    float tilt = 0.015 * sin(u_time * 0.61 + 1.0) - 0.09 * u_think
               + (0.13 + 0.05 * u_level) * u_listen;
    float lift = 0.012 * sin(u_time * 1.7 - 0.8) + 0.03 * u_think + 0.02 * talk;
    float stretch = 1.0 + 0.012 * breath + 0.015 * talk;
    vec2 b = tx_rot(p - foot, lean);
    b.y /= stretch;
    vec2 neck = vec2(0.0, 1.02);
    vec2 h = tx_rot(b - neck, tilt) - vec2(0.0, 0.22 + lift);
    vec2 hc = neck + tx_rot(vec2(0.0, 0.22 + lift), -tilt);    // head centre, back in actor space
    hc = foot + tx_rot(hc * vec2(1.0, stretch), -lean);

    // The face slides across the head toward whatever has its attention.
    vec2 f = h - vec2(-0.035 * u_listen + 0.02 * u_think, 0.03 * u_think - 0.01 * u_listen);
    vec2 fa = vec2(abs(f.x), f.y);

    vec3 sky = vec3(0.36, 0.66, 1.0);
    vec3 ink = vec3(0.07, 0.08, 0.11);
    vec3 coat = vec3(0.10, 0.11, 0.15);
    vec3 snow = vec3(0.98, 0.98, 1.0);
    vec3 shade = vec3(0.80, 0.85, 0.94);
    vec3 orange = vec3(1.0, 0.74, 0.13);
    vec3 amber = vec3(0.92, 0.50, 0.06);

    vec4 acc = vec4(0.0);

    // Listening: three arcs by the ear with a wave of brightness running in toward it. The level
    // pushes them out, lengthens and thickens them, and turns them up.
    if (u_listen > 0.002) {
        vec2 v = p - (hc + vec2(-0.16, 0.03));
        float r = length(v);
        float c = dot(v, vec2(-0.985, 0.17)) / max(r, 0.001);
        float a = 0.0;
        for (int i = 0; i < 3; i++) {
            float fi = float(i);
            float rad = 0.29 + 0.02 * u_level + (0.062 + 0.042 * u_level) * fi;
            float wid = 0.012 + 0.014 * u_level;
            float span = mix(0.82, 0.58, u_level) + 0.03 * fi;
            float wave = 0.5 + 0.5 * sin(u_time * 4.0 + fi * 1.4);
            float ring = 1.0 - smoothstep(wid * 0.4, wid * 1.5, abs(r - rad));
            a = max(a, ring * smoothstep(span, span + 0.14, c) * (0.45 + 0.55 * wave));
        }
        a *= smoothstep(-1.0, -0.92, p.x);        // fade out rather than hit the actor edge
        acc = tx_over(acc, sky, a * u_listen * (0.35 + 0.65 * u_level));
    }

    // Thinking: three dots pulse in turn above the head, following its curve.
    if (u_think > 0.002) {
        for (int i = 0; i < 3; i++) {
            float fi = float(i);
            float s = 0.5 + 0.5 * sin(u_time * 4.2 - fi * 1.15);
            vec2 c = hc + vec2((fi - 1.0) * 0.2, 0.50 + 0.025 * s - 0.045 * (fi - 1.0) * (fi - 1.0));
            float d = length(p - c) - (0.038 + 0.026 * s);
            acc = tx_over(acc, sky, u_think * (0.55 + 0.45 * s) * tx_cov(d, ppu));
        }
    }

    // One silhouette for coat and flippers: a single clean outer edge. The body is a pear, widest
    // where it sits.
    float wave = 0.2 * talk * sin(u_time * 9.0);
    vec2 bq = b - vec2(0.0, 0.58);
    float sil = tx_smin(tx_ell(bq, vec2(0.61 - 0.20 * bq.y, 0.58)),
                        tx_ell(h, vec2(0.37, 0.40)), 0.30);
    float swing = 0.60 + 0.03 * breath + wave;
    float flips = min(tx_flipper(b - vec2(-0.40, 0.88), -swing - 0.16 * u_listen),
                      tx_flipper(b - vec2(0.40, 0.88), swing + 0.1 + 0.05 * u_listen));
    sil = tx_smin(sil, flips, 0.05);

    // A thin cool rim and a sheen keep the black readable on dark wallpapers.
    vec3 rim = vec3(0.33, 0.38, 0.50);
    vec3 col = mix(coat, rim, 0.45 * smoothstep(-0.022, -0.01, sil));
    float gloss = 1.0 - smoothstep(0.004, 0.03, abs(tx_ell(h, vec2(0.31, 0.345))));
    float toward = dot(h, vec2(-0.55, 0.835)) / max(length(h), 0.001);
    col = mix(col, rim, 0.5 * gloss * smoothstep(0.72, 0.96, toward));

    // White front, narrowing up to the chin.
    vec2 bl = b - vec2(0.0, 0.56);
    float belly = tx_ell(bl, vec2(0.475 - 0.14 * bl.y, 0.535));
    col = mix(col, mix(snow, shade, 0.8 * smoothstep(-0.14, 0.0, belly)), tx_cov(belly, ppu));

    // Eyes: tall white ovals set straight into the black head. Two uneven clocks stop the slow
    // blink feeling mechanical. The upper lid sweeps down to a shallow arc and the lower lid rises
    // to meet it, so a shut eye is a curved line.
    float blink = 1.0 - min(smoothstep(0.04, 0.30, abs(mod(u_time, 5.3) - 0.9)),
                            smoothstep(0.04, 0.30, abs(mod(u_time, 12.7) - 3.9)));
    vec2 gaze = vec2(0.012 * u_think - 0.024 * u_listen, 0.04 * u_think);
    vec2 e = tx_rot(fa - vec2(0.125, 0.09), 0.10);
    vec2 er = vec2(0.112, 0.155);
    float dome = sqrt(max(1.0 - e.x * e.x / (er.x * er.x), 0.0));
    float eye = max(tx_ell(e, er), max(e.y - mix(1.0, -0.42, blink) * er.y * dome,
                                       -mix(1.0, 0.42, blink) * er.y * dome - e.y));
    col = mix(col, mix(snow, shade, 0.55 * smoothstep(-0.05, 0.0, eye)), tx_cov(eye, ppu));
    vec2 ey = vec2(f.x - sign(f.x) * 0.10, f.y - 0.075) - gaze;
    col = mix(col, ink, tx_cov(max(tx_ell(ey, vec2(0.052, 0.074)), eye), ppu));
    col = mix(col, snow, tx_cov(max(length(ey - vec2(-0.016, 0.028)) - 0.017, eye), ppu));

    // Beak: broad and golden. The mouth opens behind the upper half as the lower jaw drops with
    // the level, and the corners of the upper half turn up into the smile.
    vec2 k = f - vec2(0.0, -0.105);
    float drop = 0.11 * talk;
    float inside = min(talk * 10.0, 1.0);
    vec2 kl = k + vec2(0.0, 0.035 + 0.5 * drop);
    float jaw = tx_ell(kl, vec2(0.15, 0.055 + 0.5 * drop));
    col = mix(col, amber, tx_cov(jaw, ppu));
    float mouth = tx_ell(kl - vec2(0.0, 0.012), vec2(0.12, 0.03 + 0.5 * drop));
    col = mix(col, vec3(0.36, 0.10, 0.14), inside * tx_cov(mouth, ppu));
    float tongue = max(tx_ell(kl + vec2(0.0, 0.012 + 0.5 * drop), vec2(0.07, 0.042)), mouth);
    col = mix(col, vec3(0.95, 0.42, 0.46), inside * tx_cov(tongue, ppu));
    vec2 ku = vec2(k.x / max(1.0 - 2.2 * k.y, 0.5), k.y - 0.6 * k.x * k.x - 0.012);
    float upper = tx_ell(ku, vec2(0.225, 0.08));
    col = mix(col, vec3(0.50, 0.24, 0.03), tx_cov(upper - 0.012, ppu));
    col = mix(col, mix(amber, orange, smoothstep(-0.06, 0.05, k.y)), tx_cov(upper, ppu));

    vec4 outc = tx_over(acc, col, tx_cov(sil, ppu));

    // Feet stay put in actor space, out in front of the body.
    vec2 ft = tx_rot(vec2(abs(p.x - foot.x), p.y) - vec2(0.37, -0.77), -0.12);
    return tx_over(outc, mix(amber, orange, smoothstep(-0.10, 0.06, ft.y)), tx_cov(tx_foot(ft), ppu));
}
