// Orb: a dark glass sphere with a four-strand braid of luminous silk sealed inside it.
//
// The braid is a wavy great circle on a shell inside the sphere. The refracted view ray crosses
// that shell twice: the front hit is sharp, the back hit is wide and dim, which reads as depth
// of field for a few sines per pixel instead of a raymarch. Listening lets the braid wander and
// swell. Speaking swings it edge-on so it reads as a waveform driven by the voice. Thinking
// straightens the strands into one circle, then crossfades to that circle solved on its own
// plane: the same curve, but a clean ellipse where a shell would pinch it at the silhouette.
//
// Every rate is a whole multiple of orb_t, one turn per 240 s, so the motion loops without a
// seam and sine arguments stay small however long the session runs. Rates are never scaled by
// u_level or a state blend, because rate * time leaps when the rate moves: going faster means
// fading in a faster wave.

const float ORB_TAU = 6.2831853;
const float ORB_RADIUS = 0.76;   // sphere radius; the actor's short half side is 1.0
const float ORB_HALO = 0.985;    // the halo reaches exactly zero here
const float ORB_RING = 0.66;     // ring radius, as a fraction of the sphere

float orb_t = 0.0;       // animation phase, 0..TAU
float orb_amp = 0.0;     // how far strands swing off the centre line
float orb_fast = 0.0;    // fast wave, only present with sound
float orb_push = 0.0;    // bounded phase shove from the audio level
float orb_wide = 1.0;    // ribbon half width
float orb_nest = 0.0;    // strand spacing once they lie side by side in the ring
float orb_ring = 0.0;    // 0 free ribbons, 1 ring
float orb_flat = 0.0;    // crossfade: 0 strands on the shell, 1 strands on the ring's plane
float orb_warm = 0.0;    // colour lean: -1 cyan leads, 1 magenta leads

vec3 orb_ax = vec3(0.0, 0.0, 1.0);  // the braid's frame: circle normal...
vec3 orb_ux = vec3(1.0, 0.0, 0.0);  // ...and the two vectors spanning its plane
vec3 orb_vx = vec3(0.0, 1.0, 0.0);

// One curve through the palette: 0 cyan, 0.33 blue, 0.66 violet, 1 magenta.
vec3 orb_hue(float h) {
    float c = 1.0 - h;
    return vec3(h * sqrt(h), 0.95 * c * c * c, 1.0 - 0.3 * h * h * h);
}

void orb_frame(float tilt, float roll) {
    float cx = cos(tilt), sx = sin(tilt), cz = cos(roll), sz = sin(roll);
    orb_ux = vec3(cz, sz, 0.0);
    orb_vx = vec3(-sz * cx, cz * cx, sx);
    orb_ax = vec3(sz * sx, -cz * sx, cx);
}

// Light from the braid at unit direction w. off moves the point off the circle. soft > 1
// defocuses. near, -1..1, makes the ring's near side wider and brighter.
vec3 orb_braid(vec3 w, float off, float near, float soft) {
    float u = dot(w, orb_ux);
    float v = dot(w, orb_vx);
    float h = dot(w, orb_ax) + off;
    float th = atan(v, u);
    float blur = soft * (1.0 + 0.3 * near);
    float mid = orb_amp * 0.55 * sin(2.0 * th - orb_t * 17.0 - orb_push);
    float turn = th - orb_t * 37.0;

    vec3 light = vec3(0.0);
    for (int i = 0; i < 4; i++) {
        float k = float(i);
        float ph = k * 1.5707963;
        float a = 3.0 * th + ph + orb_t * 37.0 + orb_push;
        float d = h + (k - 1.5) * orb_nest - mid - orb_amp * sin(a)
                - orb_fast * sin(5.0 * th + 2.0 * ph - orb_t * 113.0);

        // The strip twists: edge-on it pinches and burns hotter, and its lit edge swaps sides.
        // A defocused strand does neither, or it would read as a scratch.
        float tw = mix(sin(2.0 * th + 1.7 * ph + orb_t * 23.0), 0.8, orb_ring);
        float face = 1.0 - 0.78 * (1.0 - tw * tw) / soft;
        float x = d / (orb_wide * face * blur);

        // Curtain profile: a tight lit edge falling away into a long veil with faint ridges.
        float y = x * (1.0 + 0.5 * tw * x / (soft * sqrt(x * x + 0.2)));
        float y2 = y * y;
        float yv = y * (0.5 + 0.5 * face);  // the veil pinches less, or it ends in a hard wedge
        float sheet = exp(-yv * yv);
        float bloom = exp(-4.0 * y2);
        float thread = bloom * bloom;
        float tail = 1.0 / (1.0 + y2 * 0.45);
        float fibre = 0.84 + 0.16 * cos(y * 4.5 + ph);

        // Bright crests travel the circle, one per strand. Over/under sells the weave.
        float along = 0.5 + 0.5 * cos(turn + ph * 1.5);
        float crest = along * along * along;
        float over = mix(0.72 + 0.28 * cos(a), 1.0, orb_ring);
        float lum = (mix(0.42, 0.16, orb_ring) + mix(0.75, 1.9, orb_ring) * crest) * over
                  * (1.0 + 0.4 * near);

        // Neighbours in the braid get far-apart hues; cyan and magenta lead.
        float hue = fract(k * 0.66);
        float lead = (0.85 + 0.6 * abs(2.0 * hue - 1.0)) * (1.0 + 0.7 * orb_warm * (hue - 0.5));
        vec3 core = orb_hue(hue);
        vec3 veil = orb_hue(clamp(hue + 0.16 * sin(th + ph), 0.0, 1.0));
        float heat = 0.5 / (0.34 + face);
        light += lum * lead * heat
               * (veil * (0.8 * sheet * fibre + 0.3 * bloom + mix(0.012, 0.09, orb_ring) * tail)
                  + mix(core, vec3(1.0), 0.5) * thread * 1.6 / (soft * soft));
    }
    // th spins around the circle's poles; nothing should reach them to show it.
    return light * smoothstep(0.15, 0.6, length(vec2(u, v)));
}

// The braid along the view ray from e: both crossings of a shell of radius s, the crossing of
// the ring's plane, or a blend of the two.
vec3 orb_layer(vec3 e, vec3 d, float s) {
    vec3 light = vec3(0.0);
    float b = dot(e, d);
    float disc = b * b - 1.0 + s * s;
    if (orb_flat < 0.99 && disc > 0.0) {
        float root = sqrt(disc);
        vec3 front = (e + d * (-b - root)) / s;
        vec3 back = (e + d * (-b + root)) / s;
        // Strands defocus by their foreshortening, so one wrapping the shell's silhouette
        // dissolves instead of bunching into a wire.
        float soft = 1.0 / (0.3 + 0.7 * root / s);
        light = (orb_braid(front, 0.0, 0.0, soft)
                 + mix(0.13, 0.6, orb_ring) * orb_braid(back, 0.0, 0.0, soft * mix(1.5, 1.35, orb_ring)))
              * smoothstep(0.0, 0.14, disc) * (1.0 - orb_flat);
    }
    if (orb_flat > 0.01) {
        vec3 x = vec3(e.xy, -dot(e.xy, orb_ax.xy) / orb_ax.z);
        float len = length(x);
        vec3 w = x / max(len, 0.001);
        light += orb_flat * orb_braid(w, len - ORB_RING, w.z, 1.0);
    }
    return light;
}

// Hue-preserving exposure: colour stays saturated until it is really hot.
vec3 orb_expose(vec3 c) {
    float m = max(c.r, max(c.g, c.b));
    vec3 keep = c * ((1.0 - exp(-1.6 * m)) / max(m, 0.0001));
    return mix(keep, vec3(1.0), 0.75 * smoothstep(1.6, 6.0, m));
}

vec4 tux_theme(vec2 uv) {
    vec2 size = max(u_size, vec2(1.0));
    float span = min(size.x, size.y);
    vec2 p = (uv * 2.0 - 1.0) * size / span;
    p.y = -p.y;
    float r = length(p);
    if (r >= ORB_HALO) return vec4(0.0);

    orb_t = fract(u_time / 240.0) * ORB_TAU;
    float lvl = clamp(u_level, 0.0, 1.0);
    float listen = clamp(u_listen, 0.0, 1.0);
    float speak = clamp(u_speak, 0.0, 1.0);
    float think = clamp(u_think, 0.0, 1.0);
    float hear = listen * lvl;
    float talk = speak * lvl;
    float energy = min(hear + talk, 1.0);
    orb_ring = think * think * (3.0 - 2.0 * think);
    orb_flat = smoothstep(0.5, 1.0, orb_ring);

    float radius = ORB_RADIUS * (mix(0.84, 1.0, u_appear) + 0.02 * hear + 0.045 * talk);
    float edge = clamp((radius - r) * span * 0.33 + 0.5, 0.0, 1.0);

    // Halo: colour drifts around the sphere and gains with sound.
    vec2 dir = p / max(r, 0.001);
    float drift = orb_t * 7.0;
    vec2 sweep = vec2(cos(drift), sin(drift));
    vec3 tint = orb_hue(0.5 + 0.48 * dot(dir, sweep));
    float g = 1.0 - max(r - radius, 0.0) / (ORB_HALO - radius);
    float g2 = g * g;
    vec3 outer = tint * (0.6 * g2 + 0.4 * g2 * g2 * g2) * (0.22 + 0.12 * orb_ring + 0.55 * energy);

    vec3 rgb = outer;
    float alpha = max(outer.r, max(outer.g, outer.b));

    if (edge > 0.0) {
        vec2 nd = p / radius;
        float rr = min(dot(nd, nd), 1.0);
        float rn = sqrt(rr);
        float nz = sqrt(1.0 - rr);
        vec3 n = vec3(nd, nz);
        vec3 ray = refract(vec3(0.0, 0.0, -1.0), n, mix(0.74, 0.97, orb_ring));

        orb_amp = mix(mix(0.2 + 0.2 * hear, 0.05 + 0.42 * lvl, speak), 0.02, orb_ring);
        orb_fast = 0.13 * energy * (1.0 - orb_ring);
        orb_push = 0.5 * energy;
        orb_wide = mix(0.13 + 0.09 * hear + 0.05 * talk, 0.06, orb_ring);
        orb_nest = 0.05 * orb_ring;
        orb_warm = speak - 0.6 * listen;

        // The braid wanders while free, turns edge-on to speak, settles onto one ring to think.
        // Roll wanders instead of winding up: blending a state away from an angle that has
        // grown with time would spin the braid through many turns in a fraction of a second.
        float roam = 1.1 * sin(orb_t * 3.0) + 0.7 * sin(orb_t * 5.0 + 1.0);
        float tilt = mix(1.3 + 0.2 * sin(orb_t * 5.0), 1.47, speak);
        orb_frame(mix(tilt, 0.85 + 0.07 * sin(orb_t * 11.0), orb_ring),
                  mix(mix(roam, 0.12 * sin(orb_t * 7.0), speak), roam, orb_ring));
        vec3 light = orb_layer(n, ray, mix(0.84, ORB_RING * 0.97, orb_ring));

        // The glass wall: ribbons stop short of the rim, a dark moat that reads as thickness.
        light *= 1.0 - smoothstep(0.87, 0.985, rn);

        // Speech also sends soft rings out from the core.
        float beat = 0.7 + 0.3 * sin(rn * 9.0 - orb_t * 83.0);
        float core = exp(-rr * 3.2) * (0.03 + 0.05 * orb_ring + 0.16 * energy + 0.2 * talk * beat);
        light = light * (1.0 + 0.4 * hear + 0.9 * talk) + orb_hue(0.55 + 0.3 * sin(drift)) * core;

        // Glass: deep body, soft rim light, a window reflection, a lower caustic.
        float fres = 1.0 - nz;
        float f2 = fres * fres;
        float f3 = f2 * fres;
        vec3 deep = mix(vec3(0.004, 0.005, 0.014), vec3(0.03, 0.024, 0.075), f2);
        vec3 rim = tint * (0.1 * f3 + 0.5 * f3 * f3 * f3 * f3) * (0.8 + 0.45 * energy);
        float up = dot(dir, vec2(-0.5, 0.866));
        float arc = smoothstep(0.62, 0.92, rn) * (1.0 - smoothstep(0.92, 0.985, rn));
        float pane = smoothstep(0.82, 0.87, rn) * (1.0 - smoothstep(0.89, 0.93, rn))
                   * smoothstep(0.55, 0.95, up);
        float gloss = arc * arc * smoothstep(0.35, 1.0, up) * 0.08 + pane * 0.11;
        float bounce = smoothstep(0.45, 0.97, rn) * smoothstep(0.1, 1.0, -up);
        vec3 glow = orb_hue(0.5 - 0.45 * dot(dir, sweep.yx)) * bounce * (0.09 + 0.22 * energy);

        vec3 inner = orb_expose(light + rim + glow) + deep + vec3(0.72, 0.82, 1.0) * gloss;
        rgb = mix(rgb, inner, edge);
        alpha = mix(alpha, mix(0.95, 1.0, f2), edge);
    }

    // Dither hides banding in the dark gradients; it fades out with coverage so empty stays 0.
    vec2 cell = floor(uv * size);
    float grain = fract(52.9829189 * fract(dot(cell, vec2(0.06711056, 0.00583715)))) - 0.5;
    rgb = clamp(rgb + grain * min(alpha * 8.0, 1.0) / 255.0, 0.0, 1.0);
    alpha = max(alpha, max(rgb.r, max(rgb.g, rgb.b)));
    return vec4(rgb, alpha);
}
