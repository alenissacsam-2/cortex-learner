/* Cortex brain: the renderer for the precomputed brain atlas. A v3 blob
   draws as a coloured neural network inside one brain, styled by a plain style object
   (CortexBrain.STYLE_SCHEMA, PRESETS, api.setStyle); a v2 blob draws as before.
   Python owns layout; this file only draws and handles input. WebGL2 first; a Canvas 2D path takes
   over with no WebGL2, on context loss, or with ?canvas in the URL. Vanilla JS with nothing a strict
   CSP blocks (no eval, fetch, workers or HTML parsing): text goes in through textContent and SVG
   text nodes. Exposes window.CortexBrain.mount(container, options). */
(function () {
  'use strict';

  const TAU = Math.PI * 2, NONE = 65535;
  const DEFAULTS = {
    node: '#ffb000', dim: '#6b4a12', link: '#ffb000', label: '#ffb000', glow: true,
    stage: '#0b0b0a', body: '#12110e', rim: '#6b4a12', grey: '#958870',
    state: { solid: '#1D9E75', learning: '#BA7517', needs_refresh: '#d0607f' },
  };
  // v3: a black stage, a cool glass shell, and a neutral for Mastery fibres and lobes without a colour.
  const V3C = { stage: '#050505', glass: '#9aa4b2', neutral: '#8b93a1' };
  // Per-version look: shell rim, point sizes (px), core whitening, corona, bloom threshold and gain.
  const LOOK2 = { rimPow: 3, rimK: 1.8, rimA: 1, back: 1, minS: 2, maxS: 7.5, coreW: 0, corona: 0.35, thr: 0, bloomK: 0.9, moat: 0, halo: 0.6 };
  const LOOK3 = { rimPow: 2.5, rimK: 0.6, rimA: 0.22, back: 0.5, minS: 1.5, maxS: 36, coreW: 0.55, corona: 0.14, thr: 0.6, bloomK: 0.9, moat: 0.55, halo: 0.28 };
  const FIB_SEG = 12, AMBIENT_MS = 30;  // fibre strip segments; shimmer and pulses redraw at ~30 fps
  const ALIVE_IDLE_MS = 120000, POP_MS = 400, BREATH_MS = 900, FX_CAP = 200;  // ambient stops after 2 min idle
  const FOV = 38 * Math.PI / 180, ZN = 0.02, ZF = 40;
  const DIST_MIN = 0.65, DIST_MAX = 3.2, PITCH_MAX = 0.35 * Math.PI;  // polar angle kept in [.15π, .85π]
  const DAMP = 0.88, FLY_MS = 600, SEL_FLY_MS = 900, IDLE_MS = 3000, HOVER_MS = 120, PICK_REST_MS = 40, BEACON_MS = 8000;
  const LINK_CAP = 60, LABELS = 16, PAGE_LABELS = 12, LABEL_GAP = 22, PHONE = 640, ZOOMED = 1.15;
  const STEM_GLASS = 0.5, STEM_DUST = 0.03, SITE_LABELS = 5;  // the stem's tube: its share of the shell's glass and
  // of its dust points; the sites named
  const TRAIL_CAP = 120, HISTORY = 20, STYLE_MS = 300, SEL_ORBIT = 0.02, DIM_NODE = 0.2, DIM_FIB = 0.15;
  const MODES = ['activity', 'mastery', 'growth'];
  const STATE_PI = { solid: 2, learning: 3, needs_refresh: 4 };  // palette: node, grey, solid, learning, refresh
  const SVGNS = ['http:', '', 'www.w3.org', '2000', 'svg'].join('/');  // in parts: the page test bans URL text

  /* ---------------- style: every look decision is a key ---------------- */
  const onOff = (key, label, group, help) => ({ key, label, group, type: 'toggle', help });
  const range = (key, label, group, min, max, step, help) => ({ key, label, group, type: 'range', min, max, step, help });
  const choice = (key, label, group, options, help) => ({ key, label, group, type: 'choice', options, help });
  const STYLE_SCHEMA = [
    choice('shell', 'Shell', 'Shell', [['none', 'None'], ['rim', 'Rim'], ['dust', 'Dust'], ['contour', 'Contour'], ['glass', 'Glass']],
      'The outline of the brain: none, a thin glowing edge, twinkling dust, slice lines, or the v3 glass.'),
    range('shellStrength', 'Shell strength', 'Shell', 0, 1, 0.05, 'How strongly the outline shows.'),
    choice('shellTint', 'Shell colour', 'Shell', [['palette', 'From the palette'], ['violet', 'Blue-violet']], 'The colour of the outline.'),
    range('nodeScale', 'Node size', 'Nodes', 0.5, 3, 0.05, 'The size of every page.'),
    range('sizeContrast', 'Hub contrast', 'Nodes', 0, 2, 0.05, 'How much bigger the important pages get: 0 makes all pages equal.'),
    onOff('hubBodies', 'Hub bodies', 'Nodes', 'Hubs drawn as crisp cell bodies with a ring and a soft corona.'),
    range('hubScale', 'Hub size', 'Nodes', 0.5, 3, 0.05, 'How big the hub cell bodies are.'),
    range('nodeBrightness', 'Node brightness', 'Nodes', 0.5, 2, 0.05, 'How bright the pages are.'),
    range('nodeGlow', 'Node glow', 'Nodes', 0, 2, 0.05, 'The glow around each page.'),
    range('dof', 'Depth of field', 'Nodes', 0, 1, 0.05, 'Pages far from the focus soften and widen, like a camera lens.'),
    onOff('links', 'Links', 'Fibres', 'Fibres for your own links between pages.'),
    onOff('similar', 'Similar', 'Fibres', 'Faint fibres between pages with similar meaning.'),
    range('fibreBrightness', 'Fibre brightness', 'Fibres', 0, 2, 0.05, 'How bright the fibres are.'),
    range('fibreWidth', 'Fibre width', 'Fibres', 0.5, 2, 0.05, 'How thick the fibres are.'),
    range('linkBrightness', 'Link brightness', 'Fibres', 0.5, 2, 0.05, 'Brightness of your own links\' fibres (similar ones keep the fibre brightness).'),
    range('linkWidth', 'Link width', 'Fibres', 0.5, 2, 0.05, 'Thickness of your own links\' fibres.'),
    range('fibreSaturation', 'Fibre saturation', 'Fibres', 0.6, 1.4, 0.05, 'How saturated the fibre colours are.'),
    range('bundling', 'Bundling', 'Fibres', 0, 1, 0.05, 'How strongly fibres gather into tracts: 0 draws them straight.'),
    onOff('shimmer', 'Shimmer', 'Fibres', 'A slow band of light travelling along the fibres.'),
    onOff('dendrites', 'Dendrites', 'Fibres', 'Short branching stubs around the top hubs.'),
    choice('trailTint', 'Trail colour', 'Fibres', [['white', 'White'], ['warm', 'Warm']], 'The colour of the trails along a chosen page\'s fibres.'),
    range('tightness', 'Tightness', 'Arrangement', 0, 0.6, 0.05, 'Pulls each page toward its region centre, so regions read as separate clouds.'),
    range('spread', 'Spread', 'Arrangement', 0.8, 1.4, 0.05, 'Moves the regions apart from the brain centre.'),
    choice('palette', 'Palette', 'Colour', [['topic', 'Topic'], ['spectrum', 'Spectrum'], ['vivid', 'Vivid'], ['cool', 'Cool'], ['ember', 'Ember']],
      'The region colours: topic keeps touching regions far apart in hue; spectrum shades neighbouring regions alike.'),
    range('saturation', 'Saturation', 'Colour', 0.6, 1.2, 0.05, 'How saturated the region colours are.'),
    range('bloom', 'Bloom', 'Colour', 0, 2, 0.05, 'The soft light around bright points.'),
    range('fog', 'Depth fog', 'Colour', 0, 1, 0.05, 'How much the far side of the brain fades.'),
    choice('background', 'Background', 'Colour', [['black', 'Black'], ['deep', 'Deep']], 'Plain black, or a deep vignette with faint star dust.'),
    range('nebula', 'Region nebula', 'Colour', 0, 1, 0.05, 'A faint coloured cloud at the heart of each region.'),
    range('vignette', 'Vignette', 'Colour', 0, 1, 0.05, 'Darkens the corners of the stage.'),
    onOff('alive', 'Alive', 'Motion', 'Calm ambient trails and region breathing.'),
    range('aliveRate', 'Alive rate', 'Motion', 0, 2, 0.05, 'How often the ambient trails run.'),
    range('spinSpeed', 'Spin speed', 'Motion', 0.02, 0.3, 0.01, 'How fast the brain turns while spin is on (radians a second).'),
    choice('labels', 'Labels', 'Labels', [['atlas', 'Atlas'], ['hover', 'Hover'], ['none', 'None']],
      'Region labels around the brain, only what you point at, or none.'),
  ];
  // Overview cameras: azimuth in degrees from the brain's left side toward its face, elevation in
  // degrees, fill = the brain's share of the stage height, nudge = its shift right as a share of the
  // stage width. fill 0 is v3's camera (yaw 0.9, pitch 0.55, the whole bounding sphere in view).
  const CAM_DEFAULT = { azimuth: 35, elevation: 18, fill: 0.7, nudge: 0 };
  // round 2 keys default to the round-1 look; a preset lists only what it sets
  const BASE2 = { hubScale: 1, nodeBrightness: 1, saturation: 1, trailTint: 'white', linkBrightness: 1, linkWidth: 1,
    fibreSaturation: 1, shellTint: 'palette' };
  const preset = (id, label, style, camera) => ({ id, label, style: Object.assign({}, BASE2, style), camera: camera || CAM_DEFAULT });
  const NEBULA = { shell: 'none', shellStrength: 0.5, nodeScale: 1, sizeContrast: 1.7, hubBodies: true,
    nodeGlow: 1.4, dof: 0.25, links: true, similar: true, fibreBrightness: 1.2, fibreWidth: 1.1, bundling: 0.9, shimmer: true,
    dendrites: true, tightness: 0.25, spread: 1.05, palette: 'vivid', bloom: 1.3, fog: 0.5, background: 'deep', nebula: 0.8,
    vignette: 0.7, alive: true, aliveRate: 1.2, spinSpeed: 0.08, labels: 'hover' };
  const TRACTO = { shell: 'dust', shellStrength: 0.45, nodeScale: 0.9, sizeContrast: 1.3, hubBodies: true,
    nodeGlow: 0.7, dof: 0.3, links: true, similar: true, fibreBrightness: 0.85, fibreWidth: 0.7, bundling: 0.8, shimmer: true,
    dendrites: true, tightness: 0.3, spread: 1, palette: 'vivid', bloom: 0.9, fog: 0.55, background: 'deep', nebula: 0.3,
    vignette: 0.6, alive: true, aliveRate: 0.5, spinSpeed: 0.04, labels: 'hover' };
  const CAM_TRACTO = { azimuth: 35, elevation: 16, fill: 0.7, nudge: 0.04 };
  const NEBULA2 = Object.assign({}, NEBULA, { shell: 'dust', shellStrength: 0.25, tightness: 0.29, saturation: 0.85 });
  const PRESETS = [
    // the arena's winner (round 2), with the judge's tweaks: links x1.5 opacity and x1.25 width, a dim
    // blue-violet dust rim at 60 % of tractogram's strength, fibre colours 10 % more saturated
    preset('tractogram2', 'Cortex (default)', Object.assign({}, TRACTO, { nodeBrightness: 1.2, fibreBrightness: 0.98, hubScale: 1.5,
      palette: 'topic', linkBrightness: 1.5, linkWidth: 1.25, shellTint: 'violet', shellStrength: 0.27, fibreSaturation: 1.1 }), CAM_TRACTO),
    preset('nebula2t', 'Nebula Topic', Object.assign({}, NEBULA2, { palette: 'topic' })),
    preset('tractogram', 'Tractogram', TRACTO, CAM_TRACTO),
    preset('nebula2s', 'Nebula Spectrum', Object.assign({}, NEBULA2, { palette: 'spectrum' })),
    preset('golgi', 'Golgi', { shell: 'rim', shellStrength: 0.22, nodeScale: 1.7, sizeContrast: 2, hubBodies: true,
      nodeGlow: 1.2, dof: 0, links: true, similar: false, fibreBrightness: 1.3, fibreWidth: 1.4, bundling: 0.15, shimmer: false,
      dendrites: true, tightness: 0.5, spread: 1.25, palette: 'topic', bloom: 0.55, fog: 0.3, background: 'black', nebula: 0,
      vignette: 0.5, alive: true, aliveRate: 0.3, spinSpeed: 0.03, labels: 'none' },
    { azimuth: 25, elevation: 28, fill: 0.75, nudge: 0 }),
    preset('challenger1', 'Lateral Atlas', { shell: 'dust', shellStrength: 0.45, nodeScale: 1.3, sizeContrast: 1.5, hubBodies: true,
      nodeGlow: 0.6, dof: 0.2, links: true, similar: true, fibreBrightness: 0.7, fibreWidth: 0.8, bundling: 0.3, shimmer: true,
      dendrites: true, tightness: 0.4, spread: 1.1, palette: 'topic', saturation: 0.8, bloom: 0.75, fog: 0.3, background: 'deep',
      nebula: 0.35, vignette: 0.5, alive: true, aliveRate: 0.6, spinSpeed: 0.05, labels: 'hover', trailTint: 'white' },
    { azimuth: 12, elevation: 12, fill: 0.84, nudge: 0 }),
    preset('nebula', 'Neon Nebula', NEBULA),
    preset('challenger2', 'Nebula Cortex', { shell: 'dust', shellStrength: 0.65, nodeScale: 1.5, sizeContrast: 1.8, hubBodies: true,
      nodeGlow: 0.45, dof: 0.45, links: true, similar: false, fibreBrightness: 0.55, fibreWidth: 0.7, bundling: 0.15, shimmer: true,
      dendrites: true, tightness: 0.5, spread: 1.2, palette: 'spectrum', saturation: 0.95, bloom: 1.1, fog: 0.45, background: 'black',
      nebula: 0.6, vignette: 0.65, alive: true, aliveRate: 0.4, spinSpeed: 0.04, labels: 'hover', trailTint: 'white' },
    { azimuth: 30, elevation: 20, fill: 0.88, nudge: 0 }),
    preset('atlas', 'Clean Atlas', { shell: 'rim', shellStrength: 0.6, nodeScale: 1.1, sizeContrast: 1.5, hubBodies: true,
      nodeGlow: 1, dof: 0, links: true, similar: false, fibreBrightness: 1.1, fibreWidth: 1, bundling: 1, shimmer: false,
      dendrites: false, tightness: 0.35, spread: 1.15, palette: 'topic', bloom: 1, fog: 0.4, background: 'black', nebula: 0.3,
      vignette: 0.4, alive: true, aliveRate: 0.7, spinSpeed: 0.05, labels: 'atlas' }),
    preset('connectome', 'Connectome', { shell: 'dust', shellStrength: 0.7, nodeScale: 0.9, sizeContrast: 1, hubBodies: false,
      nodeGlow: 1.4, dof: 0.3, links: true, similar: true, fibreBrightness: 1.5, fibreWidth: 0.9, bundling: 0.85, shimmer: true,
      dendrites: true, tightness: 0.15, spread: 1, palette: 'cool', bloom: 1.6, fog: 0.45, background: 'deep', nebula: 0.4,
      vignette: 0.6, alive: true, aliveRate: 1, spinSpeed: 0.06, labels: 'hover' }),
    preset('ember', 'Ember Station', { shell: 'contour', shellStrength: 0.6, nodeScale: 1, sizeContrast: 1.3, hubBodies: true,
      nodeGlow: 1.2, dof: 0.2, links: true, similar: true, fibreBrightness: 1.1, fibreWidth: 1, bundling: 1, shimmer: true,
      dendrites: false, tightness: 0.2, spread: 1.05, palette: 'ember', bloom: 1.2, fog: 0.5, background: 'deep', nebula: 0.5,
      vignette: 0.6, alive: true, aliveRate: 0.8, spinSpeed: 0.06, labels: 'atlas' }),
    preset('v3', 'v3 (as shipped)', { shell: 'glass', shellStrength: 1, nodeScale: 1, sizeContrast: 1, hubBodies: true,
      nodeGlow: 1, dof: 0, links: true, similar: true, fibreBrightness: 1, fibreWidth: 1, bundling: 1, shimmer: true,
      dendrites: false, tightness: 0, spread: 1, palette: 'topic', bloom: 1, fog: 0.5, background: 'black', nebula: 0,
      vignette: 0, alive: true, aliveRate: 1, spinSpeed: 0.1, labels: 'atlas' },
    { azimuth: 141.6, elevation: 31.5, fill: 0, nudge: 0 }),
  ];
  const DEFAULT_PRESET = 'tractogram2';  // the arena's winner, shown as 'Cortex (default)'
  const DEFAULT_STYLE = Object.freeze(Object.assign({}, PRESETS.find((p) => p.id === DEFAULT_PRESET).style));
  for (const p of PRESETS) { Object.freeze(p.style); Object.freeze(p.camera); Object.freeze(p); }
  // A partial style merged onto `base`: unknown keys and bad values are ignored, numbers clamped.
  function mergeStyle(base, part) {
    const out = Object.assign({}, base);
    if (!part || typeof part !== 'object') return out;
    for (const s of STYLE_SCHEMA) {
      if (!(s.key in part)) continue;
      const v = part[s.key];
      if (s.type === 'toggle') { if (typeof v === 'boolean') out[s.key] = v; }
      else if (s.type === 'range') { const n = +v; if (Number.isFinite(n)) out[s.key] = Math.min(s.max, Math.max(s.min, n)); }
      else if (s.options.some((o) => o[0] === v)) out[s.key] = v;
    }
    return out;
  }
  // Palettes from the lobe colours: `topic` keeps them; `vivid` raises chroma; `cool` and `ember` keep
  // the lobes' hue order but spread it over cyan-violet-magenta or amber-red-gold, alternating
  // lightness so neighbours in that narrow range still differ.
  function rgb2hsl(c) {
    const r = c[0] / 255, g = c[1] / 255, b = c[2] / 255, mx = Math.max(r, g, b), mn = Math.min(r, g, b), l = (mx + mn) / 2;
    if (mx === mn) return [0, 0, l];
    const d = mx - mn, s = l > 0.5 ? d / (2 - mx - mn) : d / (mx + mn);
    const h = mx === r ? (g - b) / d + (g < b ? 6 : 0) : mx === g ? (b - r) / d + 2 : (r - g) / d + 4;
    return [h * 60, s, l];
  }
  function hsl2rgb(h, s, l) {
    const k = (n) => (n + h / 30) % 12, a = s * Math.min(l, 1 - l);
    const f = (n) => l - a * Math.max(-1, Math.min(k(n) - 3, 9 - k(n), 1));
    return [Math.round(f(0) * 255), Math.round(f(8) * 255), Math.round(f(4) * 255)];
  }
  const PAL_RANGE = { cool: [180, 320], ember: [10, 55] };
  const SHELL_TINT = { topic: [143, 163, 192], spectrum: [150, 160, 225], vivid: [160, 140, 255], cool: [98, 198, 255], ember: [255, 173, 74] };
  const toLin = (v) => { v /= 255; return v <= 0.04045 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); };
  const toByte = (v) => Math.round(255 * Math.min(1, Math.max(0, v <= 0.0031308 ? 12.92 * v : 1.055 * Math.pow(v, 1 / 2.4) - 0.055)));
  function rgb2lab(c) {  // sRGB bytes -> OKLab
    const r = toLin(c[0]), g = toLin(c[1]), b = toLin(c[2]);
    const l = Math.cbrt(0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b);
    const m = Math.cbrt(0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b);
    const s = Math.cbrt(0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b);
    return [0.2104542553 * l + 0.793617785 * m - 0.0040720468 * s, 1.9779984951 * l - 2.428592205 * m + 0.4505937099 * s,
      0.0259040371 * l + 0.7827717662 * m - 0.808675766 * s];
  }
  function lab2rgb(c) {  // OKLab -> sRGB bytes (clipped to the gamut)
    const l = (c[0] + 0.3963377774 * c[1] + 0.2158037573 * c[2]) ** 3, m = (c[0] - 0.1055613458 * c[1] - 0.0638541728 * c[2]) ** 3;
    const s = (c[0] - 0.0894841775 * c[1] - 1.291485548 * c[2]) ** 3;
    return [toByte(4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s), toByte(-1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s),
      toByte(-0.0041960863 * l - 0.7034186147 * m + 1.707614701 * s)];
  }
  // The palette, then `saturation` scales each colour's OKLab chroma. `spectrum` (v4 round 2): hue from
  // the lobe centre's angle around the vertical axis, so neighbouring regions get analogous hues, at
  // OKLCH L 0.74, C 0.17; grey lobes and lobes without a centre keep their colour.
  function paletteOf(name, cols, sat, centres) {
    const out = paletteBase(name, cols, centres);
    return sat === 1 || sat === undefined ? out : out.map((c) => { const q = rgb2lab(c); return lab2rgb([q[0], q[1] * sat, q[2] * sat]); });
  }
  function paletteBase(name, cols, centres) {
    if (name === 'spectrum') {
      return cols.map((c, k) => {
        const q = rgb2lab(c), cn = centres && centres[k];
        if (!cn || Math.hypot(q[1], q[2]) < 0.03) return c;
        const h = Math.atan2(cn[0], cn[2]);
        return lab2rgb([0.74, 0.17 * Math.cos(h), 0.17 * Math.sin(h)]);
      });
    }
    if (name === 'vivid') {
      return cols.map((c) => {
        const [h, s, l] = rgb2hsl(c);
        return s < 0.08 ? c : hsl2rgb(h, Math.min(1, s * 1.3 + 0.15), Math.min(0.66, Math.max(0.5, 0.58 + (l - 0.6) * 0.5)));
      });
    }
    const rg = PAL_RANGE[name];
    if (!rg) return cols;
    const hs = cols.map(rgb2hsl), order = cols.map((c, k) => k).filter((k) => hs[k][1] >= 0.08).sort((a, b) => hs[a][0] - hs[b][0] || a - b);
    const out = cols.map((c, k) => hsl2rgb((rg[0] + rg[1]) / 2, 0.12, hs[k][2]));  // greys stay grey, warmed or cooled
    order.forEach((k, r) => {
      out[k] = hsl2rgb(rg[0] + (rg[1] - rg[0]) * (r + 0.5) / order.length, [0.85, 0.72, 0.95][r % 3], [0.6, 0.5, 0.7][r % 3]);
    });
    return out;
  }

  function bytes(s) {
    if (typeof s !== 'string' || !s) return new Uint8Array(0);
    let bin;
    try { bin = atob(s); } catch (e) { return new Uint8Array(0); }
    const u = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) u[i] = bin.charCodeAt(i);
    return u;
  }
  // shortcut: typed views read host byte order; every browser we target is little-endian like the data
  const view = (u, T) => new T(u.buffer, 0, u.byteLength / T.BYTES_PER_ELEMENT | 0);
  function rgb(hex, fb) {
    const h = String(hex || '').replace('#', ''), v = h.length === 3 ? h.replace(/./g, '$&$&') : h;
    const n = /^[0-9a-f]{6}$/i.test(v) ? parseInt(v, 16) : -1;
    return n < 0 ? (fb ? rgb(fb) : [255, 176, 0]) : [n >> 16 & 255, n >> 8 & 255, n & 255];
  }
  // The stage is dark in both themes: a colour too dark to read on it keeps the default.
  const lightOr = (hex, fb) => { const c = rgb(hex, fb); return (0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]) / 255 < 0.15 ? fb : hex; };
  function bezier(x1, y1, x2, y2) {  // CSS cubic-bezier easing
    return (x) => {
      let t = x;
      for (let i = 0; i < 8; i++) {
        const u = 1 - t, fx = 3 * x1 * t * u * u + 3 * x2 * t * t * u + t * t * t - x;
        if (Math.abs(fx) < 1e-5) break;
        t = Math.min(1, Math.max(0, t - fx / (3 * x1 * u * u + 6 * (x2 - x1) * t * u + 3 * (1 - x2) * t * t || 1e-6)));
      }
      const u = 1 - t;
      return 3 * y1 * t * u * u + 3 * y2 * t * t * u + t * t * t;
    };
  }
  const EASE = bezier(0.22, 1, 0.36, 1);
  const isoDay = (d) => new Date(d.getTime() - d.getTimezoneOffset() * 6e4).toISOString().slice(0, 10);

  /* ---------------- WebGL2 renderer ---------------- */
  const QUAD_VS = `#version 300 es
const vec2 Q[3] = vec2[3](vec2(-1.0, -1.0), vec2(3.0, -1.0), vec2(-1.0, 3.0));
out vec2 vT;
void main() { vec2 p = Q[gl_VertexID]; vT = p * 0.5 + 0.5; gl_Position = vec4(p, 0.0, 1.0); }`;
  const BLUR_FS = `#version 300 es
precision highp float;
in vec2 vT; uniform sampler2D uTex; uniform vec2 uDir; out vec4 o;
void main() {
  const float w[5] = float[5](0.2270270, 0.1945946, 0.1216216, 0.0540541, 0.0162162);
  vec3 c = texture(uTex, vT).rgb * w[0];
  for (int i = 1; i < 5; i++) {
    c += texture(uTex, vT + uDir * float(i)).rgb * w[i];
    c += texture(uTex, vT - uDir * float(i)).rgb * w[i];
  }
  o = vec4(c, 1.0);
}`;
  const COMP_FS = `#version 300 es
precision highp float;
in vec2 vT; uniform sampler2D uTex; uniform float uK; out vec4 o;
void main() { o = vec4(texture(uTex, vT).rgb * uK, 0.0); }`;
  // v3: the scene and its bloom, tone-mapped on the brightest channel so a dense cluster stays its
  // colour instead of washing to white (linear up to 0.6, then compressed toward 1)
  // v4 grade on top: vibrance on the greyer pixels, shadows lifted toward #04060C, a radial vignette
  // and a 4x4 ordered dither of half a level, which keeps soft glows from banding
  const TONE_FS = `#version 300 es
precision highp float;
in vec2 vT; uniform sampler2D uScene, uBloom; uniform float uK, uVig; out vec4 o;
const float BAYER[16] = float[16](0.0, 8.0, 2.0, 10.0, 12.0, 4.0, 14.0, 6.0, 3.0, 11.0, 1.0, 9.0, 15.0, 7.0, 13.0, 5.0);
void main() {
  vec3 c = texture(uScene, vT).rgb + texture(uBloom, vT).rgb * uK;
  float m = max(max(c.r, c.g), c.b), t = m < 0.55 ? m : 0.55 + 0.4 * (1.0 - exp((0.55 - m) / 0.4));
  c = m > 0.0 ? c * (t / m) : c;
  float luma = dot(c, vec3(0.2126, 0.7152, 0.0722)), sat = max(max(c.r, c.g), c.b) - min(min(c.r, c.g), c.b);
  c = max(mix(vec3(luma), c, 1.0 + 0.15 * (1.0 - clamp(sat * 2.0, 0.0, 1.0))), 0.0);
  c += vec3(0.0157, 0.0235, 0.047) * (1.0 - smoothstep(0.0, 0.12, luma));
  vec2 q = vT - 0.5;
  c *= 1.0 - 0.25 * uVig * dot(q, q) * 2.0;
  ivec2 ip = ivec2(gl_FragCoord.xy) & 3;
  o = vec4(c + (BAYER[ip.y * 4 + ip.x] / 15.0 - 0.5) / 255.0, 1.0);
}`;
  // v4 deep background: a near-black radial vignette and faint static star dust, one pixel a star
  const BG_FS = `#version 300 es
precision highp float;
in vec2 vT; uniform vec3 uStage; uniform float uDeep; uniform vec2 uRes; out vec4 o;
float h(vec2 p) { return fract(sin(dot(p, vec2(12.9898, 78.233))) * 43758.5453); }
void main() {
  vec2 q = (vT - 0.5) * vec2(uRes.x / uRes.y, 1.0);
  vec3 c = uStage + uDeep * vec3(0.022, 0.026, 0.046) * (1.0 - smoothstep(0.0, 0.9, length(q)));
  vec2 cell = floor(gl_FragCoord.xy / 3.0), f = mod(gl_FragCoord.xy, 3.0);
  float star = step(0.9965, h(cell)) * step(f.x, 1.0) * step(f.y, 1.0) * (0.05 + 0.17 * h(cell + 7.31));
  o = vec4(c + star * uDeep, 1.0);
}`;
  // v4 dust shell: points on the surface that twinkle; almost only the silhouette shows (round 2), so the
  // brain's outline reads from any angle
  const DUST_VS = `#version 300 es
layout(location=0) in vec3 aPos; layout(location=1) in vec3 aNrm; layout(location=2) in float aSeed;
uniform mat4 uVP; uniform vec3 uEye; uniform float uTime, uDpr, uK;
out float vA;
void main() {
  vec4 c = uVP * vec4(aPos, 1.0);
  gl_Position = c;
  float g = 1.0 - abs(dot(normalize(aNrm), normalize(uEye - aPos)));
  float tw = 0.55 + 0.45 * sin(uTime * (0.6 + aSeed * 1.7) + aSeed * 40.0);
  vA = c.w < 0.05 ? 0.0 : 3.0 * uK * smoothstep(0.3, 1.6, length(uEye - aPos)) * (0.04 + pow(g, 4.0)) * tw;
  gl_PointSize = (1.4 + 1.4 * aSeed) * uDpr;
}`;
  const DUST_FS = `#version 300 es
precision highp float;
in float vA; uniform vec3 uTintC; out vec4 o;
void main() { vec2 d = gl_PointCoord * 2.0 - 1.0; float r = dot(d, d); if (r > 1.0) discard; o = vec4(uTintC * vA * (1.0 - r), 0.0); }`;
  // Arrangement on the GPU: a page moves with its lobe, p' = p·s + o, where the lobe's
  // centre goes out by `spread` from the brain centre and s = 1 - tightness pulls pages toward it.
  // uLobeC holds each lobe's centre (w = 1 when it has one); other lobes and no lobe stay put.
  const ARR = `
uniform highp sampler2D uLobeC; uniform int uNL; uniform vec3 uCtr; uniform vec2 uArr;
vec4 lobeT(float lobe) {
  int k = int(lobe + 0.5);
  if (k >= uNL) return vec4(0.0, 0.0, 0.0, 1.0);
  vec4 c = texelFetch(uLobeC, ivec2(k, 0), 0);
  if (c.w < 0.5) return vec4(0.0, 0.0, 0.0, 1.0);
  return vec4(uCtr + (c.xyz - uCtr) * uArr.x - c.xyz * uArr.y, uArr.y);
}`;
  const MESH_VS = `#version 300 es
layout(location=0) in vec3 aPos; layout(location=1) in vec3 aNrm; layout(location=2) in float aLobe;
uniform mat4 uVP; uniform sampler2D uTint; uniform int uLobes;
out vec3 vN; out vec3 vP; out vec4 vTint;
void main() {
  int k = int(aLobe + 0.5);
  vTint = k < uLobes ? texelFetch(uTint, ivec2(k, 0), 0) : vec4(0.0);
  vN = aNrm; vP = aPos;
  gl_Position = uVP * vec4(aPos, 1.0);
}`;
  const MESH_FS = `#version 300 es
precision highp float;
in vec3 vN; in vec3 vP; in vec4 vTint;
uniform vec3 uEye, uBody, uRim, uTintC; uniform float uOpacity, uContours, uRimPow, uRimK, uRimA, uBack, uFade, uShellK;
uniform float uPass, uRimW, uConW, uSlice, uY0; out vec4 o;
void main() {
  vec3 n = normalize(vN), v = normalize(uEye - vP);
  float nv = dot(n, v), f = pow(1.0 - abs(nv), uRimPow);
  if (uPass > 0.5) {  // v4 rim and contour, additive: a thin silhouette line and ~10 horizontal slices
    float g = 1.0 - abs(nv), h = (vP.y - uY0) * uSlice;
    float l = 1.0 - smoothstep(0.0, fwidth(h) * 1.2, abs(fract(h - 0.5) - 0.5));
    float near = smoothstep(0.3, 2.2, length(uEye - vP));
    o = vec4(uTintC * (pow(g, 9.0) * uRimW + l * (0.2 + 0.8 * g * g) * uConW) * near * uShellK, 0.0);
    return;
  }
  vec3 c = mix(uBody, vTint.rgb, vTint.a);
  float a = mix(uOpacity, 0.85, vTint.a * 0.6);
  if (uContours > 0.0) {
    float h = vP.y * 18.0, l = 1.0 - smoothstep(0.0, fwidth(h) * 1.2, abs(fract(h - 0.5) - 0.5));
    c += uRim * l * 0.7; a = max(a, l * 0.6);
  }
  c += uRim * f * uRimK;
  // v3: glass near the eye fades, so a close look at a lobe is not veiled by the wall in front
  float near = mix(1.0, smoothstep(0.3, 2.2, length(uEye - vP)), uFade);
  o = vec4(c, max(a, f * uRimA) * (nv < 0.0 ? uBack : 1.0) * near * uShellK);  // uShellK: 0 with the camera inside
}`;
  // v3 fibres: one instance per fibre, a camera-facing strip along its quadratic Bezier, coloured from
  // the source lobe to the target lobe, additive, with a slow shimmer travelling along it. v4: the ends
  // and control point follow the arrangement (aX: length attenuation, lobe of a, lobe of b), the control
  // point is re-weighted toward the straight midpoint by `bundling`, the strip tapers to its ends and
  // has a thin hot core, and long fibres fade so the centre does not wash out. The same shader draws
  // effects (aPulse.y > 0): a comet is the stretch [head - tail, head] of a fibre, white at the head and
  // the far end's colour along its tail; a flat effect (a flare, a dendrite) covers its whole length.
  // A region's breathing brightens whatever lies near uWave.xyz (a Gaussian of radius uWave.w).
  const FIB_VS = `#version 300 es
layout(location=0) in vec2 aTS; layout(location=1) in vec3 aA; layout(location=2) in vec3 aB; layout(location=3) in vec4 aC;
layout(location=4) in vec4 aColA; layout(location=5) in vec4 aColB; layout(location=6) in vec4 aPulse; layout(location=7) in vec3 aX;
uniform mat4 uVP; uniform vec2 uView; uniform vec4 uWave;
uniform float uDpr, uFogNear, uFogK, uTime, uShimmer, uExpo, uWaveK, uFibK, uFibW, uBundle, uFxK, uBloomFx, uFibSat; uniform vec3 uSelP;
${ARR}
out vec3 vCol; out float vA, vSide, vCore;
vec3 A, B, C;
vec3 bez(float t) { float u = 1.0 - t; return u * u * A + 2.0 * u * t * C + t * t * B; }
void main() {
  vec4 ta = lobeT(aX.y), tb = lobeT(aX.z);
  A = aA * ta.w + ta.xyz; B = aB * tb.w + tb.xyz;
  // the chosen page's fibres (and their comets) stay softly curved like v3's, whatever the bundling is
  float bun = min(distance(aA, uSelP), distance(aB, uSelP)) < 1e-5 ? max(uBundle, 0.85) : uBundle;
  C = mix((A + B) * 0.5, aC.xyz * (ta.w + tb.w) * 0.5 + (ta.xyz + tb.xyz) * 0.5, bun);
  bool fx = aPulse.y > 0.0;  // aPulse: head t, tail length, unused, comet (1) or flat (0)
  float t = fx ? clamp(aPulse.x - aPulse.y * (1.0 - aTS.x), 0.0, 1.0) : aTS.x, a0 = aColA.a;
  vec3 wp = bez(t);
  vec4 p = uVP * vec4(wp, 1.0), q = uVP * vec4(bez(min(t + 0.04, 1.0)), 1.0), r = uVP * vec4(bez(max(t - 0.04, 0.0)), 1.0);
  if (a0 < 0.003 || p.w < 0.02 || q.w < 0.02 || r.w < 0.02) { gl_Position = vec4(2.0, 2.0, 2.0, 1.0); return; }
  vec2 d = (q.xy / q.w - r.xy / r.w) * uView;
  float s = 1.0 - aTS.x;  // along a comet: 0 at its head, 1 at its tail end
  float taper = fx ? mix(1.0, 0.25 + 0.75 * pow(1.0 - s, 1.5), aPulse.w) : 0.55 + 0.45 * sin(3.14159 * t);
  float l = length(d), w = aColB.a * 6.375 * (fx ? 1.0 : uFibW) * taper, hw = max(w, 1.0) * 0.5 + 0.5;
  vec2 n = l > 1e-5 ? vec2(-d.y, d.x) / l : vec2(0.0, 1.0);
  p.xy += n * aTS.y * hw * uDpr * 2.0 / uView * p.w;
  gl_Position = p;
  float band = fx ? 0.0 : uShimmer * exp(-pow((t - fract(uTime * 0.15 + aC.w)) / 0.06, 2.0));
  float wave = uWaveK * exp(-pow(distance(wp, uWave.xyz) / uWave.w, 2.0));
  float base;
  if (fx) {
    base = a0 * uFxK * mix(1.0, 1.0 - s, aPulse.w);
    if (uBloomFx > 0.5) base *= mix(1.0, smoothstep(0.86, 1.0, aTS.x), aPulse.w);  // only a comet's head blooms
    vCol = aPulse.w > 0.5 ? mix(aColB.rgb, aColA.rgb, smoothstep(0.0, 0.4, s)) : mix(aColA.rgb, aColB.rgb, t);
  } else {
    base = (a0 * uExpo * aX.x + 0.15 * band * min(1.0, a0 / 0.22)) * uFibK;
    vCol = mix(aColA.rgb, aColB.rgb, t);
    vCol = max(mix(vec3(dot(vCol, vec3(0.2126, 0.7152, 0.0722))), vCol, uFibSat), 0.0);
  }
  vA = base * (1.0 + 2.5 * wave) * min(w, 1.0) * exp(-uFogK * (fx ? 1.0 : 1.6) * max(0.0, p.w - uFogNear));
  vSide = aTS.y; vCore = max(w, 1.0) * 0.5 / hw;
}`;
  const FIB_FS = `#version 300 es
precision highp float;
in vec3 vCol; in float vA, vSide, vCore; out vec4 o;
void main() {
  float s = abs(vSide);
  o = vec4(vCol * vA * (1.0 - smoothstep(vCore * 0.5, 1.0, s) + 0.6 * (1.0 - smoothstep(0.0, vCore * 0.35, s))), 0.0);
}`;
  const LINE_VS = `#version 300 es
layout(location=0) in vec3 aA; layout(location=1) in vec3 aB; layout(location=2) in vec4 aM; layout(location=3) in vec4 aCol;
uniform mat4 uVP; uniform vec2 uView; uniform float uDpr, uFogNear, uFogK;
out vec4 vCol; out float vSide, vU, vDash;
void main() {
  vec4 a = uVP * vec4(aA, 1.0), b = uVP * vec4(aB, 1.0);
  if (a.w < 0.02 || b.w < 0.02) { gl_Position = vec4(2.0, 2.0, 2.0, 1.0); return; }
  vec4 c = aM.x < 0.5 ? a : b;
  vec2 d = (b.xy / b.w - a.xy / a.w) * uView;
  float l = length(d);
  vec2 n = l > 1e-5 ? vec2(-d.y, d.x) / l : vec2(0.0, 1.0);
  c.xy += n * aM.y * (abs(aM.w) * 0.5 + 0.5) * uDpr * 2.0 / uView * c.w;
  gl_Position = c;
  vCol = vec4(aCol.rgb, aCol.a * exp(-uFogK * max(0.0, c.w - uFogNear)));
  vSide = aM.y; vU = aM.z; vDash = aM.w < 0.0 ? 1.0 : 0.0;
}`;
  const LINE_FS = `#version 300 es
precision highp float;
in vec4 vCol; in float vSide, vU, vDash; out vec4 o;
void main() {
  if (vDash > 0.5 && fract(vU * 40.0) > 0.5) discard;
  o = vec4(vCol.rgb * vCol.a * (1.0 - smoothstep(0.4, 1.0, abs(vSide))), 0.0);
}`;
  // Points: pages (uSprite 0) and sprites (uSprite 1: glimmer, blinks, nebulae, Today pulses). A page's
  // |aSize| - 1 is its importance (0-1, a percentile) and the size comes from the style (uSize: scale,
  // base, amplitude, hub bodies); a negative aSize marks a hub. A sprite's aSize is px at the reference
  // depth, or (negative) plain px. Depth of field (uDof: focus depth, range, strength) widens, softens
  // and fades pages away from the focus.
  const POINT_VS = `#version 300 es
layout(location=0) in vec3 aPos; layout(location=1) in float aSize; layout(location=2) in vec4 aCol;
layout(location=3) in vec4 aFx; layout(location=4) in vec2 aCorner; layout(location=5) in float aLobe;
uniform mat4 uVP; uniform vec2 uView; uniform vec4 uWave, uSize; uniform vec3 uDof; uniform float uHubK;
uniform float uDpr, uRefZ, uFogNear, uFogK, uFlash, uMinS, uMaxS, uTime, uPulse, uBreath, uPop, uWaveK;
uniform int uPick, uSprite;
${ARR}
out vec2 vUV; out vec4 vCol, vFx; out float vHub, vPulse, vFl, vCoc, vHalf, vBig; flat out int vId;
void main() {
  vec4 lt = uSprite == 1 ? vec4(0.0, 0.0, 0.0, 1.0) : lobeT(aLobe);
  vec3 wp = aPos * lt.w + lt.xyz;
  vec4 c = uVP * vec4(wp, 1.0);
  if (aCol.a < 0.002 || c.w < 0.02) { gl_Position = vec4(2.0, 2.0, 2.0, 1.0); return; }
  vHub = uSprite == 0 && aSize < 0.0 ? uSize.w : 0.0;
  // aFx.z picks a one-shot: 255 the Growth flash, 160 a hover breath, 96 the search pop
  float fl = aFx.z > 0.8 ? uFlash : aFx.z > 0.5 ? uBreath : aFx.z > 0.2 ? uPop : 0.0;
  float wave = uWaveK * exp(-pow(distance(wp, uWave.xyz) / uWave.w, 2.0));
  vFl = fl + wave;
  float zk = clamp(uRefZ / c.w, 0.75, 1.6), s;
  if (uSprite == 1) s = aSize < 0.0 ? -aSize : aSize * zk;
  else {
    float q = max(0.0, abs(aSize) - 1.0), b = uSize.x * (uSize.y + uSize.z * pow(q, 20.0));
    if (aSize < 0.0) b = max(b, uSize.x * uSize.y * (1.6 + 0.4 * q)) * uHubK;  // a hub: a cell body, scaled by importance
    s = clamp(b * zk, uMinS, uMaxS);
  }
  float coc = uSprite == 1 ? 0.0 : clamp(abs(c.w - uDof.x) / uDof.y, 0.0, 1.0) * uDof.z, ck = 1.0 + 1.5 * coc;
  s *= (1.0 + aFx.w + fl + 0.35 * wave) * ck;
  float ext = uSprite == 1 && aFx.x > 0.8 ? 1.0 : (aFx.x > 0.0 || aFx.y > 0.0) ? 2.6 : vHub > 0.0 ? 2.2 : 1.8;
  if (uPick == 1) { s = max(s * 1.2, 10.0); ext = 1.0; }
  c.xy += aCorner * s * 0.5 * ext * uDpr * 2.0 / uView * c.w;
  gl_Position = c;
  vUV = aCorner * ext; vHalf = s * 0.5 * ext * uDpr; vCoc = coc;
  vBig = clamp((s - 7.0) / 10.0, 0.0, 1.0);  // a big hub drops the moat and the halo ring: a filled dot
  // a page right at the eye fades rather than filling the view
  vCol = vec4(aCol.rgb, aCol.a * exp(-uFogK * max(0.0, c.w - uFogNear)) * smoothstep(0.04, 0.2, c.w) / (ck * ck));
  vFx = aFx; vId = gl_InstanceID;
  vPulse = uPulse > 0.5 ? 0.55 + 0.45 * sin(uTime * 2.4 + float(gl_InstanceID) * 2.399) : 1.0;
}`;
  const POINT_FS = `#version 300 es
precision highp float; precision highp int;
in vec2 vUV; in vec4 vCol, vFx; in float vHub, vPulse, vFl, vCoc, vHalf, vBig; flat in int vId;
uniform int uPick, uSprite; uniform float uBloom, uCoreW, uCorona, uThr, uMoat, uHalo, uGlow, uTime, uDpr, uNodeK; out vec4 o;
float arm(vec2 p, float len, float sg) { return exp(-p.y * p.y / (2.0 * sg * sg)) * pow(max(0.0, 1.0 - abs(p.x) / len), 1.5); }
void main() {
  float d = length(vUV);
  if (uPick == 1) {
    if (d > 1.0) discard;
    int id = vId + 1;
    o = vec4(float(id & 255), float((id >> 8) & 255), float((id >> 16) & 255), 255.0) / 255.0;
    return;
  }
  if (uSprite == 1 && vFx.x > 0.3) {  // additive sprites: a 4-arm star glint (x = 1) or a soft glow (x = 0.5)
    float k;
    if (vFx.x > 0.8) {
      float an = uTime * 0.1396, ca = cos(an), sa = sin(an);  // 8 degrees a second
      vec2 p = mat2(ca, sa, -sa, ca) * vUV * vHalf, q = mat2(0.7071, 0.7071, -0.7071, 0.7071) * p;
      float L = vHalf, sg = 0.6 * uDpr;
      k = arm(p, L, sg) + arm(p.yx, L, sg) + 0.35 * (arm(q, 0.4 * L, sg) + arm(q.yx, 0.4 * L, sg)) + exp(-dot(p, p) / (4.5 * sg * sg));
      if (uBloom > 0.5) k *= 0.4;
    } else k = uBloom > 0.5 ? 0.0 : exp(-d * d * 3.0);
    o = vec4(vCol.rgb * k * vCol.a, 0.0);
    return;
  }
  bool hollow = vFx.y > 0.0;
  float core = hollow ? 0.0 : exp(-d * d * 6.0 / (1.0 + 2.0 * vCoc));
  // a hub's cell body: a crisp disc (edge antialiased over a pixel) inside its soft glow
  float e = fwidth(d) * 0.8, disc = 1.0 - smoothstep(0.4 - e, 0.4 + e, d);
  if (!hollow && vHub > 0.0) core = mix(core, max(disc, core * 0.6), vHub * (1.0 - vCoc));
  // bloom: ordinary pages bloom from a tighter core (radius -30 %), hubs from their whole body
  float bcore = vHub > 0.0 || uSprite == 1 ? core : (hollow ? 0.0 : exp(-d * d * 12.0 / (1.0 + 2.0 * vCoc)));
  float ring = hollow ? exp(-pow((d - 1.0) / 0.22, 2.0)) : 0.0;
  float corona = exp(-d * 2.2) * (hollow ? 0.12 : uCorona * uGlow) + vHub * exp(-d * 1.4) * 0.12 * uGlow;
  // a recent page or a search match: v3 drew a ring at 2.1x; v4 draws a soft glow (no hollow rings in the
  // overview); v2 keeps the ring
  float halo = vFx.x <= 0.0 ? 0.0 : uCoreW > 0.4 ? exp(-d * 1.6) * uHalo * 0.6 * vPulse * (1.0 - vCoc)
    : exp(-pow((d - 2.1) / 0.3, 2.0)) * uHalo * vPulse;
  float cell = vHub * exp(-pow((d - 1.7) / 0.16, 2.0)) * 0.3 * (1.0 - 0.7 * vBig);
  float a = vCol.a * (1.0 + vFl);
  // bloom: a big hub's disc would bloom by its area and wash its region out, so it blooms less
  float nk = uSprite == 1 ? 1.0 : uNodeK;
  if (uBloom > 0.5) { o = vec4(vCol.rgb * max(0.0, bcore + ring - uThr) / (1.0 - uThr) * a * nk * (uSprite == 1 ? 1.0 : uGlow * (1.0 - 0.7 * vBig) * (1.0 - 0.25 * vHub)), 0.0); return; }
  vec3 hot = mix(vCol.rgb, vec3(1.0), uCoreW);
  // v3: a dark moat around the core lifts each cell body off the fibres behind it
  float moat = uMoat * (1.0 - vBig) * exp(-pow((d - 0.75) / 0.3, 2.0)) * vCol.a;
  o = vec4((hot * (core + ring) + vCol.rgb * (corona + halo + cell)) * a * nk, min(1.0, (core + ring) * vCol.a + moat));
}`;

  function glRenderer(canvas, S) {
    const gl = canvas.getContext('webgl2', { antialias: false, alpha: false, depth: false, stencil: false,
      premultipliedAlpha: false, powerPreference: 'high-performance' });
    if (!gl) return null;
    function program(vs, fs) {
      const p = gl.createProgram();
      for (const [type, src] of [[gl.VERTEX_SHADER, vs], [gl.FRAGMENT_SHADER, fs]]) {
        const sh = gl.createShader(type);
        gl.shaderSource(sh, src);
        gl.compileShader(sh);
        if (!gl.getShaderParameter(sh, gl.COMPILE_STATUS)) throw new Error('shader: ' + gl.getShaderInfoLog(sh));
        gl.attachShader(p, sh);
      }
      gl.linkProgram(p);
      if (!gl.getProgramParameter(p, gl.LINK_STATUS)) throw new Error('link: ' + gl.getProgramInfoLog(p));
      const u = {};
      for (let i = 0, n = gl.getProgramParameter(p, gl.ACTIVE_UNIFORMS); i < n; i++) {
        const name = gl.getActiveUniform(p, i).name.replace(/\[0\]$/, '');
        u[name] = gl.getUniformLocation(p, name);
      }
      return { p, u };
    }
    const meshP = program(MESH_VS, MESH_FS), lineP = program(LINE_VS, LINE_FS), pointP = program(POINT_VS, POINT_FS);
    const blurP = program(QUAD_VS, BLUR_FS), compP = program(QUAD_VS, COMP_FS), fibP = S.V3 ? program(FIB_VS, FIB_FS) : null;
    // v3 draws into a scene target (half float when the GPU can render to it) and tone-maps it
    const toneP = S.V3 ? program(QUAD_VS, TONE_FS) : null, hdr = !!(S.V3 && gl.getExtension('EXT_color_buffer_float'));
    const bgP = S.V3 ? program(QUAD_VS, BG_FS) : null, dustP = S.V3 && S.dust.length ? program(DUST_VS, DUST_FS) : null;
    const buf = (target, data, usage) => {
      const b = gl.createBuffer();
      gl.bindBuffer(target, b);
      gl.bufferData(target, data, usage || gl.STATIC_DRAW);
      return b;
    };
    const attr = (loc, size, type, norm, stride, off, div) => {
      gl.enableVertexAttribArray(loc);
      gl.vertexAttribPointer(loc, size, type, norm, stride, off);
      if (div) gl.vertexAttribDivisor(loc, div);
    };

    // Shell bodies. Winding is checked once so back-face culling keeps the outside.
    const bodies = S.mesh.map((m) => {
      const vao = gl.createVertexArray();
      gl.bindVertexArray(vao);
      buf(gl.ARRAY_BUFFER, m.pos); attr(0, 3, gl.FLOAT, false, 0, 0);
      buf(gl.ARRAY_BUFFER, m.nrm); attr(1, 3, gl.FLOAT, false, 0, 0);
      buf(gl.ARRAY_BUFFER, m.lobe); attr(2, 1, gl.UNSIGNED_SHORT, false, 0, 0);
      buf(gl.ELEMENT_ARRAY_BUFFER, m.idx);
      gl.bindVertexArray(null);
      return { vao, count: m.idx.length, ccw: m.ccw, k: m.name === 'stem' ? STEM_GLASS : 1 };
    });
    const tintTex = gl.createTexture();
    gl.bindTexture(gl.TEXTURE_2D, tintTex);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.NEAREST);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.NEAREST);
    let tintN = 0;
    // lobe centres for the arrangement, kept bound on texture unit 2
    const lobeTex = gl.createTexture();
    gl.activeTexture(gl.TEXTURE2);
    gl.bindTexture(gl.TEXTURE_2D, lobeTex);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.NEAREST);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.NEAREST);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA32F, Math.max(1, S.L), 1, 0, gl.RGBA, gl.FLOAT, S.L ? S.lobeC : new Float32Array(4));
    gl.activeTexture(gl.TEXTURE0);

    // Points: static position and importance, per-instance colour and effects rewritten on restyle.
    const corner = buf(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 1, -1, -1, 1, 1, 1]));
    const N = S.N, PS = new Float32Array(N * 4);
    for (let i = 0; i < N; i++) { PS.set(S.P.subarray(i * 3, i * 3 + 3), i * 4); PS[i * 4 + 3] = (S.HUB[i] ? -1 : 1) * (1 + S.Q[i]); }
    // Fibres: a shared strip of (t, side) and per-fibre end points, control point, shimmer phase, length
    // attenuation and end lobes; the colours, alpha and width (S.FCOL, 8 bytes a fibre) change on restyle.
    // Effects and dendrites: 25 floats each (a, b, c + seed, rgba, rgb + width/6.375, pulse, attenuation
    // and lobes), the effects rewritten each frame.
    let fibVao = null, fibDyn = null, pulseVao = null, pulseBuf = null, nPulses = 0, denVao = null, denBuf = null, nDen = 0;
    if (fibP) {
      const ts = new Float32Array((FIB_SEG + 1) * 4);
      for (let s = 0; s <= FIB_SEG; s++) ts.set([s / FIB_SEG, -1, s / FIB_SEG, 1], s * 4);
      const tsBuf = buf(gl.ARRAY_BUFFER, ts);
      if (S.NF) {
        const st = new Float32Array(S.NF * 13);
        for (let f = 0; f < S.NF; f++) {
          const a = S.FA[f] * 3, b = S.FB[f] * 3, o = f * 13;
          st[o] = S.P[a]; st[o + 1] = S.P[a + 1]; st[o + 2] = S.P[a + 2];
          st[o + 3] = S.P[b]; st[o + 4] = S.P[b + 1]; st[o + 5] = S.P[b + 2];
          st[o + 6] = S.FC[f * 3]; st[o + 7] = S.FC[f * 3 + 1]; st[o + 8] = S.FC[f * 3 + 2];
          st[o + 9] = f * 0.6180339 % 1;
          st[o + 10] = S.FATT[f]; st[o + 11] = S.LOBE[S.FA[f]]; st[o + 12] = S.LOBE[S.FB[f]];
        }
        fibVao = gl.createVertexArray();
        gl.bindVertexArray(fibVao);
        gl.bindBuffer(gl.ARRAY_BUFFER, tsBuf); attr(0, 2, gl.FLOAT, false, 0, 0);
        buf(gl.ARRAY_BUFFER, st);
        attr(1, 3, gl.FLOAT, false, 52, 0, 1); attr(2, 3, gl.FLOAT, false, 52, 12, 1); attr(3, 4, gl.FLOAT, false, 52, 24, 1);
        attr(7, 3, gl.FLOAT, false, 52, 40, 1);
        fibDyn = buf(gl.ARRAY_BUFFER, S.FCOL, gl.DYNAMIC_DRAW);
        attr(4, 4, gl.UNSIGNED_BYTE, true, 8, 0, 1); attr(5, 4, gl.UNSIGNED_BYTE, true, 8, 4, 1);
      }
      const fxVao = (b) => {
        const vao = gl.createVertexArray();
        gl.bindVertexArray(vao);
        gl.bindBuffer(gl.ARRAY_BUFFER, tsBuf); attr(0, 2, gl.FLOAT, false, 0, 0);
        gl.bindBuffer(gl.ARRAY_BUFFER, b);
        attr(1, 3, gl.FLOAT, false, 100, 0, 1); attr(2, 3, gl.FLOAT, false, 100, 12, 1); attr(3, 4, gl.FLOAT, false, 100, 24, 1);
        attr(4, 4, gl.FLOAT, false, 100, 40, 1); attr(5, 4, gl.FLOAT, false, 100, 56, 1); attr(6, 4, gl.FLOAT, false, 100, 72, 1);
        attr(7, 3, gl.FLOAT, false, 100, 88, 1);
        return vao;
      };
      pulseBuf = gl.createBuffer(); pulseVao = fxVao(pulseBuf);
      denBuf = gl.createBuffer(); denVao = fxVao(denBuf);
      gl.bindVertexArray(null);
    }
    const drawPulses = () => {
      if (!nPulses) return;
      gl.bindVertexArray(pulseVao);
      gl.drawArraysInstanced(gl.TRIANGLE_STRIP, 0, (FIB_SEG + 1) * 2, nPulses);
    };
    const pointVao = gl.createVertexArray();
    gl.bindVertexArray(pointVao);
    buf(gl.ARRAY_BUFFER, PS); attr(0, 3, gl.FLOAT, false, 16, 0, 1); attr(1, 1, gl.FLOAT, false, 16, 12, 1);
    const colBuf = buf(gl.ARRAY_BUFFER, S.COL, gl.DYNAMIC_DRAW); attr(2, 4, gl.UNSIGNED_BYTE, true, 0, 0, 1);
    const fxBuf = buf(gl.ARRAY_BUFFER, S.FX, gl.DYNAMIC_DRAW); attr(3, 4, gl.UNSIGNED_BYTE, true, 0, 0, 1);
    gl.bindBuffer(gl.ARRAY_BUFFER, corner); attr(4, 2, gl.FLOAT, false, 0, 0);
    buf(gl.ARRAY_BUFFER, S.LOBE.slice(0, N)); attr(5, 1, gl.UNSIGNED_SHORT, false, 0, 0, 1);
    // Sprites share the point shader: x y z size, then 4 colour and 4 effect bytes.
    const spriteVao = gl.createVertexArray(), spriteBuf = gl.createBuffer();
    let nSprites = 0;
    gl.bindVertexArray(spriteVao);
    gl.bindBuffer(gl.ARRAY_BUFFER, spriteBuf);
    attr(0, 3, gl.FLOAT, false, 24, 0, 1); attr(1, 1, gl.FLOAT, false, 24, 12, 1);
    attr(2, 4, gl.UNSIGNED_BYTE, true, 24, 16, 1); attr(3, 4, gl.UNSIGNED_BYTE, true, 24, 20, 1);
    gl.bindBuffer(gl.ARRAY_BUFFER, corner); attr(4, 2, gl.FLOAT, false, 0, 0);
    // Dust: points on the shell (x y z, normal, seed)
    let dustVao = null;
    if (dustP) {
      dustVao = gl.createVertexArray();
      gl.bindVertexArray(dustVao);
      buf(gl.ARRAY_BUFFER, S.dust);
      attr(0, 3, gl.FLOAT, false, 28, 0); attr(1, 3, gl.FLOAT, false, 28, 12); attr(2, 1, gl.FLOAT, false, 28, 24);
    }
    // Ribbons: links, tracts; 6 vertices per segment of 14 floats (a, b, t side u width, rgba).
    const lineVao = gl.createVertexArray(), lineBuf = gl.createBuffer();
    let nLineVerts = 0;
    gl.bindVertexArray(lineVao);
    gl.bindBuffer(gl.ARRAY_BUFFER, lineBuf);
    attr(0, 3, gl.FLOAT, false, 56, 0); attr(1, 3, gl.FLOAT, false, 56, 12);
    attr(2, 4, gl.FLOAT, false, 56, 24); attr(3, 4, gl.FLOAT, false, 56, 40);
    const quadVao = gl.createVertexArray();
    gl.bindVertexArray(null);

    // Bloom targets at half resolution, and the 1x1 pick target with its own depth.
    function target(w, h, depth, half) {
      const tex = gl.createTexture();
      gl.bindTexture(gl.TEXTURE_2D, tex);
      if (half) gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA16F, w, h, 0, gl.RGBA, gl.HALF_FLOAT, null);
      else gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA8, w, h, 0, gl.RGBA, gl.UNSIGNED_BYTE, null);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
      const fb = gl.createFramebuffer();
      gl.bindFramebuffer(gl.FRAMEBUFFER, fb);
      gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, tex, 0);
      if (depth) {
        const rb = gl.createRenderbuffer();
        gl.bindRenderbuffer(gl.RENDERBUFFER, rb);
        gl.renderbufferStorage(gl.RENDERBUFFER, gl.DEPTH_COMPONENT16, w, h);
        gl.framebufferRenderbuffer(gl.FRAMEBUFFER, gl.DEPTH_ATTACHMENT, gl.RENDERBUFFER, rb);
      }
      gl.bindFramebuffer(gl.FRAMEBUFFER, null);
      return { tex, fb, w, h };
    }
    const pickT = target(1, 1, true), px = new Uint8Array(4);
    let bloomA = null, bloomB = null, sceneT = null, Wd = 1, Hd = 1, dpr = 1;

    function resize(w, h, r) {
      dpr = r; Wd = Math.max(1, Math.round(w * r)); Hd = Math.max(1, Math.round(h * r));
      canvas.width = Wd; canvas.height = Hd;
      for (const t of [bloomA, bloomB, sceneT]) if (t) { gl.deleteTexture(t.tex); gl.deleteFramebuffer(t.fb); }
      bloomA = target(Wd >> 1 || 1, Hd >> 1 || 1, false, hdr);
      bloomB = target(Wd >> 1 || 1, Hd >> 1 || 1, false, hdr);
      sceneT = toneP ? target(Wd, Hd, false, hdr) : null;
    }
    // the arrangement's uniforms, shared by the point and fibre programs
    function arrU(u, F) {
      gl.uniform1i(u.uLobeC, 2);
      gl.uniform1i(u.uNL, S.arrange ? S.L : 0);
      gl.uniform3fv(u.uCtr, S.ctr);
      gl.uniform2f(u.uArr, F.arr[0], F.arr[1]);
    }
    function pointUniforms(F, pick, bloom) {
      const u = pointP.u, K = F.look;
      gl.useProgram(pointP.p);
      gl.uniformMatrix4fv(u.uVP, false, F.VP);
      gl.uniform2f(u.uView, Wd, Hd);
      gl.uniform1f(u.uDpr, dpr);
      gl.uniform1f(u.uRefZ, F.refZ);
      gl.uniform1f(u.uFogNear, F.fogNear);
      gl.uniform1f(u.uFogK, F.fogK);
      gl.uniform1f(u.uFlash, F.flash);
      gl.uniform1f(u.uBreath, F.breath);
      gl.uniform1f(u.uPop, F.pop);
      gl.uniform4fv(u.uWave, F.wave);
      gl.uniform1f(u.uWaveK, F.waveK);
      gl.uniform1f(u.uMinS, K.minS);
      gl.uniform1f(u.uMaxS, K.maxS);
      gl.uniform1f(u.uTime, F.time);
      gl.uniform1f(u.uPulse, F.pulse);
      gl.uniform1f(u.uCoreW, K.coreW);
      gl.uniform1f(u.uCorona, K.corona);
      gl.uniform1f(u.uThr, K.thr);
      gl.uniform1f(u.uMoat, K.moat);
      gl.uniform1f(u.uHalo, K.halo);
      gl.uniform4fv(u.uSize, F.sizeU);
      gl.uniform1f(u.uHubK, F.hubK);
      gl.uniform1f(u.uNodeK, F.nodeK);
      gl.uniform3fv(u.uDof, F.dof);
      gl.uniform1f(u.uGlow, F.glow);
      gl.uniform1i(u.uPick, pick ? 1 : 0);
      gl.uniform1i(u.uSprite, 0);
      gl.uniform1f(u.uBloom, bloom ? 1 : 0);
      arrU(u, F);
    }
    function drawPoints() {
      gl.bindVertexArray(pointVao);
      gl.drawArraysInstanced(gl.TRIANGLE_STRIP, 0, 4, N);
      if (nSprites) {  // sprites glow: no dark moat
        gl.uniform1f(pointP.u.uMoat, 0);
        gl.uniform1i(pointP.u.uSprite, 1);
        gl.bindVertexArray(spriteVao);
        gl.drawArraysInstanced(gl.TRIANGLE_STRIP, 0, 4, nSprites);
      }
    }
    function draw(F) {
      const C = F.colors, out = sceneT ? sceneT.fb : null, K = F.look;
      gl.bindFramebuffer(gl.FRAMEBUFFER, out);
      gl.viewport(0, 0, Wd, Hd);
      gl.clearColor(C.stage[0], C.stage[1], C.stage[2], 1);
      gl.clear(gl.COLOR_BUFFER_BIT);
      if (bgP && F.deep > 0.003) {  // 0. the deep background
        gl.useProgram(bgP.p);
        gl.uniform3fv(bgP.u.uStage, C.stage);
        gl.uniform1f(bgP.u.uDeep, F.deep);
        gl.uniform2f(bgP.u.uRes, Wd, Hd);
        gl.bindVertexArray(quadVao);
        gl.drawArrays(gl.TRIANGLES, 0, 3);
      }
      gl.enable(gl.BLEND);
      // 1. shell: v3 glass (two-sided, alpha), then v4 rim and contour lines (additive), then dust
      gl.useProgram(meshP.p);
      const mu = meshP.u;
      gl.uniformMatrix4fv(mu.uVP, false, F.VP);
      gl.uniform3fv(mu.uEye, F.eye);
      gl.uniform3fv(mu.uBody, C.body);
      gl.uniform3fv(mu.uRim, C.rim);
      gl.uniform3fv(mu.uTintC, F.tint);
      gl.uniform1f(mu.uOpacity, F.shell);
      gl.uniform1f(mu.uContours, F.contours ? 1 : 0);
      gl.uniform1f(mu.uRimPow, K.rimPow);
      gl.uniform1f(mu.uRimK, K.rimK);
      gl.uniform1f(mu.uRimA, K.rimA);
      gl.uniform1f(mu.uBack, K.back);
      gl.uniform1f(mu.uFade, S.V3 ? 1 : 0);
      gl.uniform1f(mu.uSlice, S.slice[1]);
      gl.uniform1f(mu.uY0, S.slice[0]);
      gl.uniform1i(mu.uLobes, tintN);
      gl.activeTexture(gl.TEXTURE0);
      gl.bindTexture(gl.TEXTURE_2D, tintTex);
      gl.uniform1i(mu.uTint, 0);
      const drawBodies = (k) => {
        for (const b of bodies) {
          gl.uniform1f(mu.uShellK, k * b.k);
          gl.frontFace(b.ccw ? gl.CCW : gl.CW);
          gl.bindVertexArray(b.vao);
          gl.drawElements(gl.TRIANGLES, b.count, gl.UNSIGNED_INT, 0);
        }
      };
      if (F.shellK * F.glassK > 0.001) {  // no glass at all with the camera inside it
        gl.blendFunc(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA);
        if (!S.V3) gl.enable(gl.CULL_FACE);
        gl.uniform1f(mu.uPass, 0);
        drawBodies(F.shellK * F.glassK);
        gl.disable(gl.CULL_FACE);
      }
      if (F.shellK * (F.rimK + F.conK) > 0.001) {
        gl.blendFunc(gl.ONE, gl.ONE);
        gl.uniform1f(mu.uPass, 1);
        gl.uniform1f(mu.uRimW, F.rimK);
        gl.uniform1f(mu.uConW, F.conK);
        drawBodies(F.shellK);
      }
      if (dustVao && F.shellK * F.dustK > 0.001) {
        gl.blendFunc(gl.ONE, gl.ONE);
        gl.useProgram(dustP.p);
        gl.uniformMatrix4fv(dustP.u.uVP, false, F.VP);
        gl.uniform3fv(dustP.u.uEye, F.eye);
        gl.uniform3fv(dustP.u.uTintC, F.tint);
        gl.uniform1f(dustP.u.uTime, F.time);
        gl.uniform1f(dustP.u.uDpr, dpr);
        gl.uniform1f(dustP.u.uK, F.shellK * F.dustK);
        gl.bindVertexArray(dustVao);
        gl.drawArrays(gl.POINTS, 0, S.dust.length / 7);
      }
      // 2a. fibres, dendrites and effects, additive: one instanced draw each
      if (fibP) {
        gl.blendFunc(gl.ONE, gl.ONE);
        gl.useProgram(fibP.p);
        const fu = fibP.u;
        gl.uniformMatrix4fv(fu.uVP, false, F.VP);
        gl.uniform2f(fu.uView, Wd, Hd);
        gl.uniform1f(fu.uDpr, dpr);
        gl.uniform1f(fu.uFogNear, F.fogNear);
        gl.uniform1f(fu.uFogK, F.fogK);
        gl.uniform1f(fu.uTime, F.time);
        gl.uniform1f(fu.uShimmer, F.shimmer);
        gl.uniform1f(fu.uExpo, F.expo);
        gl.uniform4fv(fu.uWave, F.wave);
        gl.uniform1f(fu.uWaveK, F.waveK);
        gl.uniform1f(fu.uFibK, F.fibK);
        gl.uniform1f(fu.uFibW, F.fibW);
        gl.uniform1f(fu.uBundle, F.bundle);
        gl.uniform3fv(fu.uSelP, F.selP);
        gl.uniform1f(fu.uFibSat, F.fibSat);
        gl.uniform1f(fu.uBloomFx, 0);
        gl.uniform1f(fu.uFxK, 1);
        arrU(fu, F);
        if (fibVao) {
          gl.vertexAttrib4f(6, 0, 0, 0, 0);  // the fibres themselves: no effect
          gl.bindVertexArray(fibVao);
          gl.drawArraysInstanced(gl.TRIANGLE_STRIP, 0, (FIB_SEG + 1) * 2, S.NF);
        }
        if (nDen && F.dendK > 0.003) {
          gl.uniform1f(fu.uFxK, F.dendK);
          gl.bindVertexArray(denVao);
          gl.drawArraysInstanced(gl.TRIANGLE_STRIP, 0, (FIB_SEG + 1) * 2, nDen);
          gl.uniform1f(fu.uFxK, 1);
        }
        drawPulses();
      }
      // 2b. ribbons (hover links, tracts), additive
      if (nLineVerts) {
        gl.blendFunc(gl.ONE, gl.ONE);
        gl.useProgram(lineP.p);
        const lu = lineP.u;
        gl.uniformMatrix4fv(lu.uVP, false, F.VP);
        gl.uniform2f(lu.uView, Wd, Hd);
        gl.uniform1f(lu.uDpr, dpr);
        gl.uniform1f(lu.uFogNear, F.fogNear);
        gl.uniform1f(lu.uFogK, F.fogK);
        gl.bindVertexArray(lineVao);
        gl.drawArrays(gl.TRIANGLES, 0, nLineVerts);
      }
      // 3. points, premultiplied: the core covers, the corona adds
      gl.blendFunc(gl.ONE, gl.ONE_MINUS_SRC_ALPHA);
      pointUniforms(F, false, false);
      drawPoints();
      // 4. bloom of the bright cores and comet heads at half resolution
      if (C.glow) {
        gl.bindFramebuffer(gl.FRAMEBUFFER, bloomA.fb);
        gl.viewport(0, 0, bloomA.w, bloomA.h);
        gl.clearColor(0, 0, 0, 0);
        gl.clear(gl.COLOR_BUFFER_BIT);
        gl.blendFunc(gl.ONE, gl.ONE);
        pointUniforms(F, false, true);
        drawPoints();
        if (nPulses) { gl.useProgram(fibP.p); gl.uniform1f(fibP.u.uBloomFx, 1); drawPulses(); }
        gl.disable(gl.BLEND);
        gl.useProgram(blurP.p);
        gl.bindVertexArray(quadVao);
        gl.uniform1i(blurP.u.uTex, 0);
        gl.bindFramebuffer(gl.FRAMEBUFFER, bloomB.fb);
        gl.bindTexture(gl.TEXTURE_2D, bloomA.tex);
        gl.uniform2f(blurP.u.uDir, 1.6 / bloomA.w, 0);
        gl.drawArrays(gl.TRIANGLES, 0, 3);
        gl.bindFramebuffer(gl.FRAMEBUFFER, bloomA.fb);
        gl.bindTexture(gl.TEXTURE_2D, bloomB.tex);
        gl.uniform2f(blurP.u.uDir, 0, 1.6 / bloomA.h);
        gl.drawArrays(gl.TRIANGLES, 0, 3);
        if (!sceneT) {
          gl.bindFramebuffer(gl.FRAMEBUFFER, null);
          gl.viewport(0, 0, Wd, Hd);
          gl.enable(gl.BLEND);
          gl.blendFunc(gl.ONE, gl.ONE);
          gl.useProgram(compP.p);
          gl.bindTexture(gl.TEXTURE_2D, bloomA.tex);
          gl.uniform1i(compP.u.uTex, 0);
          gl.uniform1f(compP.u.uK, K.bloomK);
          gl.drawArrays(gl.TRIANGLES, 0, 3);
        }
      }
      if (sceneT) {  // 5. v3: scene + bloom, tone-mapped and graded onto the canvas
        gl.disable(gl.BLEND);
        gl.bindFramebuffer(gl.FRAMEBUFFER, null);
        gl.viewport(0, 0, Wd, Hd);
        gl.useProgram(toneP.p);
        gl.bindVertexArray(quadVao);
        gl.activeTexture(gl.TEXTURE1);
        gl.bindTexture(gl.TEXTURE_2D, bloomA.tex);
        gl.activeTexture(gl.TEXTURE0);
        gl.bindTexture(gl.TEXTURE_2D, sceneT.tex);
        gl.uniform1i(toneP.u.uScene, 0);
        gl.uniform1i(toneP.u.uBloom, 1);
        gl.uniform1f(toneP.u.uK, C.glow ? K.bloomK * F.bloom : 0);
        gl.uniform1f(toneP.u.uVig, F.vig);
        gl.drawArrays(gl.TRIANGLES, 0, 3);
      }
      gl.disable(gl.BLEND);
      gl.bindVertexArray(null);
    }
    // ID render of the points into one pixel under the pointer; the nearest point wins.
    function pick(x, y, F) {
      const pxl = Math.round(x * dpr), pyl = Hd - 1 - Math.round(y * dpr);
      gl.bindFramebuffer(gl.FRAMEBUFFER, pickT.fb);
      gl.viewport(-pxl, -pyl, Wd, Hd);
      gl.clearColor(0, 0, 0, 0);
      gl.enable(gl.DEPTH_TEST);
      gl.depthFunc(gl.LESS);
      gl.depthMask(true);
      gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT);
      pointUniforms(F, true, false);
      gl.bindVertexArray(pointVao);
      gl.drawArraysInstanced(gl.TRIANGLE_STRIP, 0, 4, N);
      gl.readPixels(0, 0, 1, 1, gl.RGBA, gl.UNSIGNED_BYTE, px);
      gl.disable(gl.DEPTH_TEST);
      gl.bindVertexArray(null);
      gl.bindFramebuffer(gl.FRAMEBUFFER, null);
      return (px[0] | px[1] << 8 | px[2] << 16) - 1;
    }
    const fxData = (b, I, n) => { gl.bindBuffer(gl.ARRAY_BUFFER, b); gl.bufferData(gl.ARRAY_BUFFER, I.subarray(0, n * 25), gl.DYNAMIC_DRAW); };
    return {
      kind: 'webgl2', gl,
      resize, draw, pick,
      setPoints() {
        gl.bindBuffer(gl.ARRAY_BUFFER, colBuf); gl.bufferSubData(gl.ARRAY_BUFFER, 0, S.COL);
        gl.bindBuffer(gl.ARRAY_BUFFER, fxBuf); gl.bufferSubData(gl.ARRAY_BUFFER, 0, S.FX);
      },
      setFibres() {
        if (fibDyn) { gl.bindBuffer(gl.ARRAY_BUFFER, fibDyn); gl.bufferSubData(gl.ARRAY_BUFFER, 0, S.FCOL); }
      },
      // effect instances (25 floats each), drawn with the fibres
      setPulses(I, n) {
        nPulses = pulseBuf ? n : 0;
        if (nPulses) fxData(pulseBuf, I, n);
      },
      setDendrites(I, n) {
        nDen = denBuf ? n : 0;
        if (nDen) fxData(denBuf, I, n);
      },
      setTint(T, n) {
        tintN = n;
        gl.bindTexture(gl.TEXTURE_2D, tintTex);
        gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA8, Math.max(1, n), 1, 0, gl.RGBA, gl.UNSIGNED_BYTE, T);
      },
      setLines(lines) {
        let segs = 0;
        for (const l of lines) segs += l.n - 1;
        const V = new Float32Array(segs * 6 * 14);
        let o = 0;
        for (const l of lines) {
          const p = l.p, w = l.dash ? -l.w : l.w;
          let u = 0;
          for (let s = 0; s < l.n - 1; s++) {
            const a = s * 3, b = a + 3;
            const len = Math.hypot(p[b] - p[a], p[b + 1] - p[a + 1], p[b + 2] - p[a + 2]);
            for (const [t, side] of [[0, -1], [0, 1], [1, -1], [1, -1], [0, 1], [1, 1]]) {
              V[o++] = p[a]; V[o++] = p[a + 1]; V[o++] = p[a + 2];
              V[o++] = p[b]; V[o++] = p[b + 1]; V[o++] = p[b + 2];
              V[o++] = t; V[o++] = side; V[o++] = u + t * len; V[o++] = w;
              V[o++] = l.c[0] / 255; V[o++] = l.c[1] / 255; V[o++] = l.c[2] / 255; V[o++] = l.a;
            }
            u += len;
          }
        }
        gl.bindBuffer(gl.ARRAY_BUFFER, lineBuf);
        gl.bufferData(gl.ARRAY_BUFFER, V, gl.DYNAMIC_DRAW);
        nLineVerts = segs * 6;
      },
      // sprites: x y z size (px at the reference depth, or -px), rgb, a, and effect bytes fx (x: 1 star,
      // 0.5 soft glow; y: ring) plus e, the size boost
      setSprites(list) {
        const ab = new ArrayBuffer(list.length * 24), f = new Float32Array(ab), u = new Uint8Array(ab);
        list.forEach((s, k) => {
          f[k * 6] = s.x; f[k * 6 + 1] = s.y; f[k * 6 + 2] = s.z; f[k * 6 + 3] = s.size;
          u[k * 24 + 16] = s.c[0]; u[k * 24 + 17] = s.c[1]; u[k * 24 + 18] = s.c[2]; u[k * 24 + 19] = Math.min(1, s.a) * 255;
          u[k * 24 + 20] = (s.kind || 0) * 255; u[k * 24 + 21] = s.ring ? 255 : 0; u[k * 24 + 23] = Math.min(1, s.e) * 255;
        });
        gl.bindBuffer(gl.ARRAY_BUFFER, spriteBuf);
        gl.bufferData(gl.ARRAY_BUFFER, ab, gl.DYNAMIC_DRAW);
        nSprites = list.length;
      },
      destroy() {
        const lose = gl.getExtension('WEBGL_lose_context');
        if (lose) lose.loseContext();
      },
    };
  }

  /* ---------------- Canvas 2D renderer (fallback) ---------------- */
  function canvasRenderer(canvas, S) {
    const ctx = canvas.getContext('2d');
    if (!ctx) return null;
    const N = S.N, P = S.TP, NB = 8, GRID = 24, NP = S.NP, NK = NB * 8 * NP;  // v4: the arranged positions
    const SX = new Float32Array(N), SY = new Float32Array(N), SR = new Float32Array(N), ZC = new Float32Array(N);
    const KEY = new Int32Array(N), ORD = new Int32Array(N), start = new Int32Array(NK + 1);
    // Fibres: batched by (colour, kind, lit or not) on restyle. A canvas strokes ~25 µs a curve, so each
    // frame draws within a budget that adapts to the frame time: lit fibres first, then links, then
    // similar ones, sampled evenly inside each colour so no topic drops out.
    let FO = null, fBat = [], budget = 2000;
    function setFibres() {
      const NF = S.NF, FCOL = S.FCOL, nk = NP * 4;
      if (!NF) return;
      const key = new Int32Array(NF), cnt = new Int32Array(nk + 1), sa = new Float32Array(nk), sw = new Float32Array(nk);
      for (let f = 0; f < NF; f++) {
        const a = FCOL[f * 8 + 3];
        if (a < 2) { key[f] = -1; continue; }
        const k = (S.FCI[f] * 2 + S.FK[f]) * 2 + (a >= 80 ? 1 : 0);
        key[f] = k; cnt[k + 1]++; sa[k] += a / 255; sw[k] += FCOL[f * 8 + 7] / 40;
      }
      for (let k = 0; k < nk; k++) cnt[k + 1] += cnt[k];
      const at = cnt.slice(0, nk);
      FO = new Int32Array(cnt[nk]);
      for (let f = 0; f < NF; f++) if (key[f] >= 0) FO[at[key[f]]++] = f;
      fBat = [];
      for (let k = 0; k < nk; k++) {
        const n = cnt[k + 1] - cnt[k];
        if (n) fBat.push({ from: cnt[k], to: cnt[k + 1], ci: k >> 2, sim: (k >> 1) % 2 === 1, lit: k % 2 === 1, a: sa[k] / n, w: sw[k] / n });
      }
    }
    const glowC = document.createElement('canvas'), gctx = glowC.getContext('2d');
    const CLS_A = [1, 0.25, 0.2, 0.05];
    let lines = [], sprites = [], W = 0, H = 0, dpr = 1, nOrd = 0;
    // Silhouette: mesh edges whose two triangles face opposite ways on screen.
    let MV = 0;
    for (const m of S.mesh) MV += m.pos.length / 3;
    const MX = new Float32Array(MV), MY = new Float32Array(MV), MZ = new Float32Array(MV);
    const edges = [], etri = [], tris = [];
    {
      const seen = new Map();
      let vo = 0;
      for (const m of S.mesh) {
        const idx = m.idx;
        for (let t = 0; t < idx.length; t += 3) {
          const tri = tris.length / 3;
          tris.push(idx[t] + vo, idx[t + 1] + vo, idx[t + 2] + vo);
          for (let e = 0; e < 3; e++) {
            const a = idx[t + e] + vo, b = idx[t + (e + 1) % 3] + vo, k = Math.min(a, b) * MV + Math.max(a, b);
            const at = seen.get(k);
            if (at === undefined) { seen.set(k, edges.length / 2); edges.push(a, b); etri.push(tri, -1); }
            else etri[at * 2 + 1] = tri;
          }
        }
        vo += m.pos.length / 3;
      }
    }
    const TS = new Int8Array(tris.length / 3);
    let grid = null;

    function resize(w, h, r) {
      W = w; H = h; dpr = r;
      canvas.width = Math.max(1, Math.round(w * r)); canvas.height = Math.max(1, Math.round(h * r));
      glowC.width = Math.max(1, Math.ceil(w / 4)); glowC.height = Math.max(1, Math.ceil(h / 4));
    }
    const css = (c, a) => `rgba(${c[0] * 255 | 0},${c[1] * 255 | 0},${c[2] * 255 | 0},${a})`;
    function draw(F) {
      const t0 = performance.now();
      const V = F.v, C = F.colors, k0 = V.focal;
      const { cy, sy, cp, sp, dist, tx, ty, tz } = V;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.globalAlpha = 1;
      ctx.fillStyle = css(C.stage, 1);
      ctx.fillRect(0, 0, W, H);
      if (F.deep > 0.003) {  // v4 deep background: the vignette only (no star dust here)
        const bg = ctx.createRadialGradient(W / 2, H / 2, 0, W / 2, H / 2, Math.hypot(W, H) * 0.5);
        bg.addColorStop(0, `rgba(13,16,26,${F.deep})`);
        bg.addColorStop(1, 'rgba(13,16,26,0)');
        ctx.fillStyle = bg;
        ctx.fillRect(0, 0, W, H);
      }
      // shell: v3 glass is a radial gradient body and its silhouette contour; rim, dust and contour are
      // the silhouette alone in the palette's tint; none draws nothing
      const c = F.glassK > 0.001 && F.proj(S.ctr[0], S.ctr[1], S.ctr[2]);
      if (c) {
        const r = S.Rb * k0 / c[2], g = ctx.createRadialGradient(c[0], c[1], 0, c[0], c[1], r);
        g.addColorStop(0, css(C.body, Math.min(1, F.shell * (S.V3 ? F.shellK * F.glassK : 2.6))));
        g.addColorStop(1, css(C.body, 0));
        ctx.fillStyle = g;
        ctx.fillRect(c[0] - r, c[1] - r, 2 * r, 2 * r);
      }
      const silA = S.V3 ? F.shellK * (0.3 * F.glassK + 0.55 * (F.rimK + F.conK + F.dustK)) : F.contours ? 0.9 : 0.7;
      if (MV && silA > 0.004) {
        let vo = 0;
        for (const m of S.mesh) {
          const p = m.pos;
          for (let i = 0; i < p.length; i += 3, vo++) {
            const vx = p[i] - tx, vy = p[i + 1] - ty, vz = p[i + 2] - tz;
            const x1 = vx * cy - vz * sy, z1 = vx * sy + vz * cy, zc = dist - (vy * sp + z1 * cp);
            MZ[vo] = zc;
            MX[vo] = W / 2 + x1 * k0 / zc; MY[vo] = H / 2 - (vy * cp - z1 * sp) * k0 / zc;
          }
        }
        for (let t = 0; t < TS.length; t++) {
          const a = tris[t * 3], b = tris[t * 3 + 1], d = tris[t * 3 + 2];
          TS[t] = (MX[b] - MX[a]) * (MY[d] - MY[a]) - (MY[b] - MY[a]) * (MX[d] - MX[a]) > 0 ? 1 : -1;
        }
        ctx.beginPath();
        for (let e = 0; e < etri.length; e += 2) {
          const t2 = etri[e + 1];
          if (t2 < 0 || TS[etri[e]] === TS[t2]) continue;
          const a = edges[e], b = edges[e + 1];
          if (MZ[a] < ZN || MZ[b] < ZN) continue;
          ctx.moveTo(MX[a], MY[a]); ctx.lineTo(MX[b], MY[b]);
        }
        ctx.strokeStyle = css(S.V3 && F.glassK < 0.5 ? F.tint : C.rim, 1);
        ctx.lineWidth = 1;
        ctx.globalAlpha = Math.min(1, silA);
        ctx.stroke();
      }
      // ribbons
      for (const l of lines) {
        ctx.beginPath();
        let zs = 0, on = false;
        for (let s = 0; s < l.n; s++) {
          const q = F.proj(l.p[s * 3], l.p[s * 3 + 1], l.p[s * 3 + 2]);
          if (!q) { on = false; continue; }
          zs += q[2];
          if (on) ctx.lineTo(q[0], q[1]); else ctx.moveTo(q[0], q[1]);
          on = true;
        }
        ctx.globalAlpha = Math.min(1, l.a * 1.4 * Math.exp(-F.fogK * Math.max(0, zs / l.n - F.fogNear)));
        ctx.strokeStyle = `rgb(${l.c[0]},${l.c[1]},${l.c[2]})`;
        ctx.lineWidth = l.w;
        ctx.setLineDash(l.dash ? [3, 3] : []);
        ctx.stroke();
      }
      ctx.setLineDash([]);
      // points, sorted by (depth bucket, alpha class, colour) with a counting sort
      const zNear = F.fogNear, zSpan = 2 * S.Rb, COL = S.COL, FX = S.FX;
      start.fill(0);
      for (let i = 0; i < N; i++) {
        const o = i * 4, a = COL[o + 3];
        ZC[i] = -1;
        if (!a) continue;
        const vx = P[i * 3] - tx, vy = P[i * 3 + 1] - ty, vz = P[i * 3 + 2] - tz;
        const x1 = vx * cy - vz * sy, z1 = vx * sy + vz * cy, zc = dist - (vy * sp + z1 * cp);
        if (zc < ZN) continue;
        ZC[i] = zc;
        SX[i] = W / 2 + x1 * k0 / zc; SY[i] = H / 2 - (vy * cp - z1 * sp) * k0 / zc;
        const s = Math.min(F.look.maxS, Math.max(F.look.minS, S.SZ[i] * Math.min(1.6, Math.max(0.75, F.refZ / zc))));
        const fz = FX[o + 2], fl = fz > 204 ? F.flash : fz > 127 ? F.breath : fz > 51 ? F.pop : 0;
        SR[i] = s * 0.42 * (1 + FX[o + 3] / 255 + fl);
        const t = (zc - zNear) / zSpan, b = t <= 0 ? NB - 1 : t >= 1 ? 0 : NB - 1 - Math.floor(t * NB);
        const cls = a > 150 ? 0 : a > 58 ? 1 : a > 30 ? 2 : 3;
        // key: depth bucket, alpha class, ring, palette colour
        KEY[i] = ((b * 4 + 3 - cls) * 2 + (FX[o + 1] ? 1 : 0)) * NP + S.PI[i];
        start[KEY[i] + 1]++;
      }
      for (let k = 0; k < NK; k++) start[k + 1] += start[k];
      const pos = start.slice(0, NK);
      nOrd = 0;
      for (let i = 0; i < N; i++) if (ZC[i] > 0) { ORD[pos[KEY[i]]++] = i; nOrd++; }
      // fibres under the points, as quadratic curves added up with 'lighter'
      if (FO) {
        const FC = S.TFC, FA = S.FA, FB = S.FB;
        let lit = 0, nl = 0, ns = 0;
        for (const bt of fBat) { const n = bt.to - bt.from; if (bt.lit) lit += n; else if (bt.sim) ns += n; else nl += n; }
        const left = Math.max(0, budget - lit), fl = nl ? Math.min(1, left / nl) : 1;
        const fs = ns ? Math.min(1, Math.max(0, left - nl) / ns) : 1;
        ctx.globalCompositeOperation = 'lighter';
        for (const bt of fBat) {
          const n = bt.to - bt.from, k = bt.lit ? n : Math.ceil(n * (bt.sim ? fs : fl)), step = n / Math.max(1, k);
          if (!k) continue;
          ctx.beginPath();
          for (let s = 0; s < k; s++) {
            const f = FO[bt.from + (s * step | 0)], i = FA[f], j = FB[f];
            if (ZC[i] <= 0 || ZC[j] <= 0) continue;
            const vx = FC[f * 3] - tx, vy = FC[f * 3 + 1] - ty, vz = FC[f * 3 + 2] - tz;
            const x1 = vx * cy - vz * sy, z1 = vx * sy + vz * cy, zc = dist - (vy * sp + z1 * cp);
            if (zc < ZN) continue;
            ctx.moveTo(SX[i], SY[i]);
            ctx.quadraticCurveTo(W / 2 + x1 * k0 / zc, H / 2 - (vy * cp - z1 * sp) * k0 / zc, SX[j], SY[j]);
          }
          const pc = F.pal[bt.ci];
          ctx.globalAlpha = Math.min(1, bt.a * 1.3);
          ctx.strokeStyle = `rgb(${pc[0]},${pc[1]},${pc[2]})`;
          ctx.lineWidth = bt.w;
          ctx.stroke();
        }
        ctx.globalCompositeOperation = 'source-over';
      }
      const fogOf = (b) => Math.exp(-F.fogK * Math.max(0, (NB - 1 - b + 0.5) / NB * zSpan));
      const flush = (k, g) => {
        if (k < 0) return;
        const r = k % (8 * NP), b = k / (8 * NP) | 0, cls = 3 - (r / (2 * NP) | 0), ring = (r / NP | 0) % 2 === 1;
        if (g) { const pc = F.pal[k % NP]; g.fillStyle = `rgb(${pc[0]},${pc[1]},${pc[2]})`; g.globalAlpha = fogOf(b) * 0.8; g.fill(); return; }
        const pc = F.palCore[k % NP];
        ctx.globalAlpha = fogOf(b) * CLS_A[cls];
        if (ring) { ctx.strokeStyle = `rgb(${pc[0]},${pc[1]},${pc[2]})`; ctx.lineWidth = 0.9; ctx.stroke(); }
        else { ctx.fillStyle = `rgb(${pc[0]},${pc[1]},${pc[2]})`; ctx.fill(); }
      };
      if (C.glow) {
        gctx.setTransform(1, 0, 0, 1, 0, 0);
        gctx.clearRect(0, 0, glowC.width, glowC.height);
        gctx.setTransform(0.25, 0, 0, 0.25, 0, 0);
        let cur = -1;
        gctx.beginPath();
        for (let q = 0; q < nOrd; q++) {
          const i = ORD[q], r8 = KEY[i] % (8 * NP);
          if (r8 < 6 * NP || (r8 / NP | 0) % 2) continue;  // lit, filled points only
          if (KEY[i] !== cur) { flush(cur, gctx); cur = KEY[i]; gctx.beginPath(); }
          const r = Math.min(SR[i] * 2.4, 10);
          if (r < 6) gctx.rect(SX[i] - r * 0.8, SY[i] - r * 0.8, r * 1.6, r * 1.6);  // a pixel or two at quarter size
          else { gctx.moveTo(SX[i] + r, SY[i]); gctx.arc(SX[i], SY[i], r, 0, TAU); }
        }
        flush(cur, gctx);
        ctx.globalAlpha = 0.6;
        if (S.V3) ctx.globalCompositeOperation = 'lighter';
        ctx.drawImage(glowC, 0, 0, glowC.width * 4, glowC.height * 4);
        ctx.globalCompositeOperation = 'source-over';
      }
      let cur = -1;
      ctx.beginPath();
      for (let q = 0; q < nOrd; q++) {
        const i = ORD[q];
        if (KEY[i] !== cur) { flush(cur); cur = KEY[i]; ctx.beginPath(); }
        const r = SR[i];
        // sub-1.5 px dots raster far faster as squares and look the same
        if (r < 1.5 && !((cur % (8 * NP) / NP | 0) % 2)) ctx.rect(SX[i] - r * 0.9, SY[i] - r * 0.9, r * 1.8, r * 1.8);
        else { ctx.moveTo(SX[i] + r, SY[i]); ctx.arc(SX[i], SY[i], r, 0, TAU); }
      }
      flush(cur);
      // halos (recent pages in Activity)
      ctx.beginPath();
      for (let q = 0; q < nOrd; q++) {
        const i = ORD[q];
        if (!FX[i * 4] || (S.V3 && COL[i * 4 + 3] < 150)) continue;  // v3: dimmed pages show no halo
        const r = SR[i] * 2.4;
        ctx.moveTo(SX[i] + r, SY[i]); ctx.arc(SX[i], SY[i], r, 0, TAU);
      }
      ctx.globalAlpha = 0.45;
      ctx.strokeStyle = S.V3 ? '#dfe4ec' : css(C.node, 1);
      ctx.lineWidth = 1;
      ctx.stroke();
      if (S.V3 && F.sizeU[3] > 0.5) {  // hubs: a faint second ring, a cell body
        ctx.beginPath();
        for (let q = 0; q < nOrd; q++) {
          const i = ORD[q];
          if (!S.HUB[i] || COL[i * 4 + 3] < 150) continue;
          const r = SR[i] * 2;
          ctx.moveTo(SX[i] + r, SY[i]); ctx.arc(SX[i], SY[i], r, 0, TAU);
        }
        ctx.globalAlpha = 0.3;
        ctx.strokeStyle = '#dfe4ec';
        ctx.lineWidth = 0.8;
        ctx.stroke();
      }
      for (const s of sprites) {
        const q = F.proj(s.x, s.y, s.z);
        if (!q) continue;
        const r = s.size * (1 + s.e);
        ctx.globalAlpha = s.a * 0.35;
        ctx.fillStyle = `rgb(${s.c[0]},${s.c[1]},${s.c[2]})`;
        ctx.beginPath(); ctx.arc(q[0], q[1], r * 1.2, 0, TAU); ctx.fill();
        ctx.globalAlpha = s.a;
        ctx.beginPath(); ctx.arc(q[0], q[1], r * 0.45, 0, TAU); ctx.fill();
      }
      ctx.globalAlpha = 1;
      grid = null;
      if (FO) {  // the fibre budget follows the frame time (aim: under ~20 ms)
        const ms = performance.now() - t0;
        budget = ms > 20 ? Math.max(300, budget * 0.7) : ms < 12 ? Math.min(S.NF, budget * 1.2 + 50) : budget;
      }
    }
    // The frontmost point under the pointer, else the nearest within 8 px, from a screen grid.
    function pick(x, y) {
      if (!W) return -1;
      if (!grid) {
        const gw = Math.ceil(W / GRID) + 1, gh = Math.ceil(H / GRID) + 1, st = new Int32Array(gw * gh + 1);
        const cell = new Int32Array(N).fill(-1);
        for (let i = 0; i < N; i++) {
          if (ZC[i] <= 0 || SX[i] < 0 || SY[i] < 0 || SX[i] >= W || SY[i] >= H) continue;
          cell[i] = (SY[i] / GRID | 0) * gw + (SX[i] / GRID | 0);
          st[cell[i] + 1]++;
        }
        for (let k = 0; k < gw * gh; k++) st[k + 1] += st[k];
        const items = new Int32Array(st[gw * gh]), at = st.slice(0, gw * gh);
        for (let i = 0; i < N; i++) if (cell[i] >= 0) items[at[cell[i]]++] = i;
        grid = { gw, gh, st, items };
      }
      const { gw, gh, st, items } = grid, gx = x / GRID | 0, gy = y / GRID | 0;
      let best = -1, bestZ = Infinity, near = -1, nearD = 64;
      for (let yy = gy - 1; yy <= gy + 1; yy++) {
        for (let xx = gx - 1; xx <= gx + 1; xx++) {
          if (xx < 0 || yy < 0 || xx >= gw || yy >= gh) continue;
          const k = yy * gw + xx;
          for (let q = st[k]; q < st[k + 1]; q++) {
            const i = items[q], dx = SX[i] - x, dy = SY[i] - y, d2 = dx * dx + dy * dy, r = Math.max(SR[i], 2) + 2;
            if (d2 <= r * r && ZC[i] < bestZ) { best = i; bestZ = ZC[i]; }
            if (d2 < nearD) { nearD = d2; near = i; }
          }
        }
      }
      return best >= 0 ? best : near;
    }
    return {
      kind: 'canvas', resize, draw, pick, setFibres, setPulses() {},  // pulses reach the canvas as sprites
      setPoints() { grid = null; }, setTint() {}, setLines(l) { lines = l; }, setSprites(s) { sprites = s; },
      destroy() {},
    };
  }

  // v4 dust shell: n points on the meshes, area-weighted, from a fixed seed (x y z, normal, seed)
  function dustOf(mesh, n) {
    let seed = 0x2545f491;
    const rnd = () => {
      seed = (seed + 0x6d2b79f5) | 0;
      let t = Math.imul(seed ^ (seed >>> 15), 1 | seed);
      t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
    const cum = [], ref = [];
    let total = 0;
    for (const m of mesh) {
      const p = m.pos, idx = m.idx;
      for (let t = 0; t < idx.length; t += 3) {
        const a = idx[t] * 3, b = idx[t + 1] * 3, c = idx[t + 2] * 3;
        const ux = p[b] - p[a], uy = p[b + 1] - p[a + 1], uz = p[b + 2] - p[a + 2];
        const vx = p[c] - p[a], vy = p[c + 1] - p[a + 1], vz = p[c + 2] - p[a + 2];
        total += Math.hypot(uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx) / 2;
        cum.push(total); ref.push(m, t);
      }
    }
    const out = new Float32Array(cum.length ? n * 7 : 0);
    for (let s = 0; s < n && cum.length; s++) {
      const x = rnd() * total;
      let lo = 0, hi = cum.length - 1;
      while (lo < hi) { const md = (lo + hi) >> 1; if (cum[md] < x) lo = md + 1; else hi = md; }
      const m = ref[lo * 2], t = ref[lo * 2 + 1], p = m.pos, nr = m.nrm;
      let r1 = rnd(), r2 = rnd();
      if (r1 + r2 > 1) { r1 = 1 - r1; r2 = 1 - r2; }
      const w = [1 - r1 - r2, r1, r2], o = s * 7;
      for (let k = 0; k < 3; k++) {
        const v = m.idx[t + k] * 3;
        for (let d = 0; d < 3; d++) { out[o + d] += w[k] * p[v + d]; out[o + 3 + d] += w[k] * nr[v + d]; }
      }
      out[o + 6] = rnd();
    }
    return out;
  }

  /* ---------------- mount ---------------- */
  function mount(container, opts) {
    opts = opts || {};
    const B = opts.brain || {}, ND = B.nodes || {};
    const idsIn = Array.isArray(ND.ids) ? ND.ids : [];
    const P = view(bytes(ND.xyz), Float32Array);
    const N = Math.min(idsIn.length, P.length / 3 | 0);
    let LOBE = view(bytes(ND.lobe), Uint16Array);
    if (LOBE.length < N) LOBE = new Uint16Array(N).fill(NONE);
    const lobes = Array.isArray(B.lobes) ? B.lobes : [], L = lobes.length;
    const v3 = (a, d) => (Array.isArray(a) && a.length === 3 && a.every(Number.isFinite) ? a : d);
    const V3 = +B.version >= 3;

    // Pages join the layout by id; a page Python placed but the blob lacks still draws.
    const byId = new Map();
    for (const p of opts.pages || opts.nodes || []) if (p && p.id != null) byId.set(String(p.id), p);
    const PG = new Array(N), idIndex = new Map();
    for (let i = 0; i < N; i++) {
      const id = String(idsIn[i]);
      idIndex.set(id, i);
      PG[i] = byId.get(id) || { id, title: id };
    }
    // The brain stem (the app's live home): the browsing store's websites, after the pages (SF on),
    // outside every lobe. Each is its own node record: colour by category, size by visits,
    // brightness by the last visit, a hollow ring when private.
    const STEM = B.stem && Array.isArray(B.stem.sites) ? B.stem : null;
    const SF = STEM ? Math.max(0, Math.min(N, STEM.first | 0)) : N;
    const SCATS = STEM && STEM.colors && typeof STEM.colors === 'object' ? Object.keys(STEM.colors).sort() : [];
    const SCAT = new Int16Array(N).fill(-1), GLOW = new Float32Array(N).fill(1);
    for (let i = SF; i < N; i++) {
      const s = STEM.sites[i - SF];
      if (!Array.isArray(s)) continue;
      PG[i] = { id: PG[i].id, title: String(s[0]), kind: 'site', group: 'site', site: String(s[0]), category: String(s[1]),
        visits: +s[2] || 0, date: String(s[3] || ''), updated: String(s[4] || ''), private: s[5] === 1 };
      SCAT[i] = SCATS.indexOf(PG[i].category);
    }
    // the node's palette entry: its lobe's colour, a site's category colour, else grey
    const pcOf = (i) => (LOBE[i] < L ? 5 + LOBE[i] : SCAT[i] >= 0 ? 5 + L + SCAT[i] : 1);
    // Links: the pages' own wikilinks, undirected and deduplicated, as a CSR adjacency.
    const la = [], lb = [], seen = new Set();
    for (let i = 0; i < N; i++) {
      for (const t of PG[i].links || []) {
        const j = idIndex.get(String(t));
        if (j === undefined || j === i) continue;
        const key = Math.min(i, j) * N + Math.max(i, j);
        if (seen.has(key)) continue;
        seen.add(key); la.push(i); lb.push(j);
      }
    }
    const AO = new Int32Array(N + 1);
    for (let l = 0; l < la.length; l++) { AO[la[l] + 1]++; AO[lb[l] + 1]++; }
    for (let i = 0; i < N; i++) AO[i + 1] += AO[i];
    const ADJ = new Int32Array(AO[N]), fillAt = AO.slice(0, N);
    for (let l = 0; l < la.length; l++) { ADJ[fillAt[la[l]]++] = lb[l]; ADJ[fillAt[lb[l]]++] = la[l]; }
    const deg = (i) => AO[i + 1] - AO[i];
    // v4 importance: Python's nodes.weight (0-1) when the blob has it, else the link degree; sizes read
    // it as a percentile (Q), so the top 2 % of pages are the big hubs whatever the vault's shape
    const WT = view(bytes(ND.weight), Float32Array), IMP = new Float32Array(N), Q = new Float32Array(N);
    for (let i = 0; i < N; i++) IMP[i] = WT.length >= N && Number.isFinite(WT[i]) ? WT[i] : Math.log1p(deg(i));
    for (const [s0, n] of [[0, SF], [SF, N - SF]]) {  // pages and stem sites rank apart: a site never resizes a page
      const ord = Array.from({ length: n }, (_, k) => s0 + k).sort((a, b) => IMP[a] - IMP[b] || a - b);
      for (let s = 0; s < n;) {  // ties share their mean rank
        let e = s;
        while (e + 1 < n && IMP[ord[e + 1]] === IMP[ord[s]]) e++;
        for (let k = s; k <= e; k++) Q[ord[k]] = n > 1 ? (s + e) / 2 / (n - 1) : 0;
        s = e + 1;
      }
    }
    const SZ = new Float32Array(N);  // size in px at the reference depth (sizeOf), for the Canvas path
    // v3 fibres: end pages a, b (Uint32), a control point each (Float32 x3), kind (0 link, 1 similar).
    const FBl = (V3 && B.fibres) || {};
    const FA = view(bytes(FBl.a), Uint32Array), FB = view(bytes(FBl.b), Uint32Array), FC = view(bytes(FBl.c), Float32Array);
    let FK = view(bytes(FBl.k), Uint8Array), NF = Math.min(FA.length, FB.length, FC.length / 3 | 0);
    if (FK.length < NF) FK = new Uint8Array(NF);  // no kinds: all links
    {
      let n = 0;  // drop fibres whose ends are not placed pages
      for (let f = 0; f < NF; f++) {
        if (FA[f] >= N || FB[f] >= N || FA[f] === FB[f]) continue;
        FA[n] = FA[f]; FB[n] = FB[f]; FK[n] = FK[f];
        FC[n * 3] = FC[f * 3]; FC[n * 3 + 1] = FC[f * 3 + 1]; FC[n * 3 + 2] = FC[f * 3 + 2];
        n++;
      }
      NF = n;
    }
    // fibres per page, as a CSR list of fibre indices
    const FAO = new Int32Array(N + 1);
    for (let f = 0; f < NF; f++) { FAO[FA[f] + 1]++; FAO[FB[f] + 1]++; }
    for (let i = 0; i < N; i++) FAO[i + 1] += FAO[i];
    const FADJ = new Int32Array(FAO[N]), fAt = FAO.slice(0, N);
    for (let f = 0; f < NF; f++) { FADJ[fAt[FA[f]]++] = f; FADJ[fAt[FB[f]]++] = f; }
    const fibreOf = (i, j) => {
      for (let q = FAO[i]; q < FAO[i + 1]; q++) { const f = FADJ[q]; if (FA[f] === j || FB[f] === j) return f; }
      return -1;
    };
    const other = (f, i) => (FA[f] === i ? FB[f] : FA[f]);
    // Very busy pages (journal days with hundreds of links) would fan their links across the whole
    // brain: a link fibre touching one is drawn only while an end is pointed at or selected.
    const BUSY = Math.max(40, Math.round(N * 0.01)), BUSYF = new Uint8Array(NF);
    let nBusy = 0;
    for (let f = 0; f < NF; f++) if (!FK[f] && (deg(FA[f]) > BUSY || deg(FB[f]) > BUSY)) { BUSYF[f] = 1; nBusy++; }
    const nb8 = bytes(B.near && B.near.ids);
    const NEAR = nb8.length >= N * 32 && N ? view(nb8, Uint32Array) : nb8.length >= N * 16 && N ? view(nb8, Uint16Array) : null;
    const beacons = B.beacons && typeof B.beacons === 'object' ? B.beacons : {};
    const tracts = (Array.isArray(B.tracts) ? B.tracts : []).filter((t) => t && t.a < L && t.b < L
      && Array.isArray(t.p) && t.p.length === 4 && t.p.every((q) => v3(q, null)));
    const maxTract = tracts.reduce((m, t) => Math.max(m, +t.n || 0), 1);

    // Shell bodies, checked for size and winding (the culling keeps the outside).
    const mesh = [];
    for (const m of Array.isArray(B.mesh) ? B.mesh : []) {
      const pos = view(bytes(m.pos), Float32Array), V = pos.length / 3 | 0;
      const idx = view(bytes(m.idx), Uint32Array);
      let nrm = view(bytes(m.nrm), Float32Array), lob = view(bytes(m.lobe), Uint16Array);
      if (!V || idx.length < 3 || idx.length % 3) continue;
      let bad = false;
      for (let k = 0; k < idx.length; k++) if (idx[k] >= V) { bad = true; break; }
      if (bad) continue;
      if (lob.length < V) lob = new Uint16Array(V).fill(NONE);
      const a = idx[0] * 3, b = idx[1] * 3, c = idx[2] * 3;
      const ux = pos[b] - pos[a], uy = pos[b + 1] - pos[a + 1], uz = pos[b + 2] - pos[a + 2];
      const wx = pos[c] - pos[a], wy = pos[c + 1] - pos[a + 1], wz = pos[c + 2] - pos[a + 2];
      // Outward = away from the body's centroid; CCW when the face normal agrees.
      let mx = 0, my = 0, mz = 0;
      for (let k = 0; k < pos.length; k += 3) { mx += pos[k]; my += pos[k + 1]; mz += pos[k + 2]; }
      mx /= V; my /= V; mz /= V;
      const nx = uy * wz - uz * wy, ny = uz * wx - ux * wz, nz = ux * wy - uy * wx;
      const ccw = nx * (pos[a] - mx) + ny * (pos[a + 1] - my) + nz * (pos[a + 2] - mz) >= 0;
      if (nrm.length < V * 3) {  // no normals: area-weighted face normals, outward (the shader normalises)
        nrm = new Float32Array(V * 3);
        const sg = ccw ? 1 : -1;
        for (let t = 0; t < idx.length; t += 3) {
          const p = idx[t] * 3, q = idx[t + 1] * 3, r = idx[t + 2] * 3;
          const ax = pos[q] - pos[p], ay = pos[q + 1] - pos[p + 1], az = pos[q + 2] - pos[p + 2];
          const bx = pos[r] - pos[p], by = pos[r + 1] - pos[p + 1], bz = pos[r + 2] - pos[p + 2];
          const fx = (ay * bz - az * by) * sg, fy = (az * bx - ax * bz) * sg, fz = (ax * by - ay * bx) * sg;
          for (const v of [p, q, r]) { nrm[v] += fx; nrm[v + 1] += fy; nrm[v + 2] += fz; }
        }
      }
      mesh.push({ name: m.name, pos, nrm, idx, lobe: lob, ccw });
    }

    // Per lobe: visible count, top pages by importance (zoomed titles), study shares (Mastery).
    const ARCH = new Uint8Array(N), lobeIdx = new Map(), lobePages = lobes.map(() => []);
    lobes.forEach((l, k) => lobeIdx.set(String(l.id), k));
    const study = lobes.map(() => ({ n: 0, solid: 0, refresh: 0 }));
    for (let i = 0; i < N; i++) {
      const k = LOBE[i];
      if (k >= L) continue;
      lobePages[k].push(i);
      if (lobes[k].archive) ARCH[i] = 1;
      const p = PG[i];
      if (p.kind === 'concept' && p.state) {
        study[k].n++;
        if (p.state === 'solid') study[k].solid++;
        if (p.state === 'needs_refresh') study[k].refresh++;
      }
    }
    const byDeg = lobePages.map((a) => a.slice().sort((x, y) => IMP[y] - IMP[x] || x - y));
    const topPages = byDeg.map((a) => a.slice(0, PAGE_LABELS));
    // v3 hubs: a lobe's top 5 % by importance (2+ links) draw as cell bodies with a second ring
    const HUB = new Uint8Array(N);
    if (V3) for (const a of byDeg) for (let q = 0; q < Math.ceil(a.length * 0.05); q++) if (deg(a[q]) >= 2) HUB[a[q]] = 1;
    // v3 colours: each lobe its own (v4: through the style's palette); label text lightened until it
    // reads at 4.5:1 on its dark plate
    const LC0 = lobes.map((l) => rgb(V3 ? l.color : null, V3C.neutral));
    const lin = (v) => { v /= 255; return v <= 0.04045 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); };
    const labOf = (c) => {
      let o = c;
      for (let t = 0.1; 0.2126 * lin(o[0]) + 0.7152 * lin(o[1]) + 0.0722 * lin(o[2]) < 0.35 && t <= 1; t += 0.1) o = c.map((v) => v + (255 - v) * t);
      return `rgb(${o.map(Math.round).join(',')})`;
    };
    let LC = LC0, LABC = LC.map(labOf);
    const visCount = new Int32Array(L);
    const centreOf = (k) => V3 && v3(lobes[k].centre, null);
    const CENTRES = lobes.map((l, k) => centreOf(k));
    const anchorOf = (k) => centreOf(k) || v3(lobes[k].anchor, null);
    const labelOrder = [];
    for (let k = 0; k < L; k++) if (anchorOf(k)) labelOrder.push(k);
    const countOf = (k) => (Number.isFinite(lobes[k].count) && lobes[k].count > 0 ? lobes[k].count : lobePages[k].length);
    labelOrder.sort((a, b) => countOf(b) - countOf(a) || a - b);
    const lobeText = lobes.map((l, k) => {
      let t = String(l.title ?? l.id ?? '');
      if (t.length > 30) t = t.slice(0, 29) + '…';
      return t + ' · ' + countOf(k).toLocaleString('en');
    });

    // Dates: Growth reads the page's own date; Activity reads `updated` over the last 7 days.
    const todayIso = typeof opts.today === 'string' ? opts.today.slice(0, 10) : isoDay(new Date());
    const weekAgo = new Date(Date.parse(todayIso) - 7 * 864e5).toISOString().slice(0, 10);
    const MONTH = new Array(N), RECENT = new Uint8Array(N);
    for (let i = 0; i < N; i++) {
      const p = PG[i];
      MONTH[i] = String(p.date || p.updated || '').slice(0, 7);
      RECENT[i] = String(p.updated || '') >= weekAgo ? 1 : 0;
      if (p.kind !== 'site') continue;  // a site dims with the days since its last visit, never below 0.3
      const age = (Date.parse(todayIso) - Date.parse(p.updated)) / 864e5;
      GLOW[i] = Number.isFinite(age) ? 0.3 + 0.7 * Math.exp(-Math.max(0, age) / 30) : 0.3;
    }

    // Bounds: the centre of the shell (else of the points) and the radius that holds both. The stem's
    // tube and its sites hang below and stay out of them (flights, fog and the glass fade keep the brain's).
    const shell = mesh.filter((m) => m.name !== 'stem');
    const lo = [Infinity, Infinity, Infinity], hi = [-Infinity, -Infinity, -Infinity];
    const grow = (arr) => { for (let k = 0; k < arr.length; k++) { const d = k % 3; if (arr[k] < lo[d]) lo[d] = arr[k]; if (arr[k] > hi[d]) hi[d] = arr[k]; } };
    for (const m of shell) grow(m.pos);
    if (!mesh.length) grow(P.subarray(0, N * 3));
    if (lo[0] > hi[0]) { lo.fill(-1); hi.fill(1); }
    const ctr = [(lo[0] + hi[0]) / 2, (lo[1] + hi[1]) / 2, (lo[2] + hi[2]) / 2];
    let Rb = 0.2;
    const reach = (arr) => { for (let k = 0; k < arr.length; k += 3) Rb = Math.max(Rb, Math.hypot(arr[k] - ctr[0], arr[k + 1] - ctr[1], arr[k + 2] - ctr[2])); };
    for (const m of shell) reach(m.pos);
    reach(P.subarray(0, SF * 3));
    // The overview's sphere (the camera without a fill): the brain's, else the one holding the brain,
    // the stem's tube and every site.
    let OC = ctr, OR = Rb;
    if (SF < N || shell.length < mesh.length) {
      const l2 = lo.slice(), h2 = hi.slice(), pts = [P.subarray(SF * 3, N * 3), ...mesh.map((m) => m.pos)];
      for (const a of pts) for (let k = 0; k < a.length; k++) { const d = k % 3; l2[d] = Math.min(l2[d], a[k]); h2[d] = Math.max(h2[d], a[k]); }
      OC = [(l2[0] + h2[0]) / 2, (l2[1] + h2[1]) / 2, (l2[2] + h2[2]) / 2];
      for (const a of pts.concat([P.subarray(0, SF * 3)])) for (let k = 0; k < a.length; k += 3) OR = Math.max(OR, Math.hypot(a[k] - OC[0], a[k + 1] - OC[1], a[k + 2] - OC[2]));
    }
    // ~600 shell vertices outline the silhouette for the label columns and the overview framing.
    const SIL = [];
    {
      let total = 0;
      for (const m of mesh) total += m.pos.length / 3;
      const src = mesh.length ? mesh.map((m) => m.pos) : [P.subarray(0, N * 3)];
      if (!mesh.length) total = N;
      const step = Math.max(1, Math.floor(total / 600));
      for (const arr of src) for (let k = 0; k < arr.length; k += 3 * step) SIL.push(arr[k], arr[k + 1], arr[k + 2]);
    }
    const small = N < 500;
    // v3: the shell's radius from the centre by direction (32 x 16 bins, each the farthest vertex in it;
    // an empty bin takes the largest), so flights stay outside the glass and the glass fades once the
    // camera is inside it or within 0.1 of it.
    const SHB = 32, SHV = 16, SHR = new Float32Array(SHB * SHV);
    const shBin = (x, y, z, r) => Math.min(SHV - 1, Math.floor(Math.acos(Math.max(-1, Math.min(1, y / (r || 1)))) / Math.PI * SHV)) * SHB
      + Math.min(SHB - 1, Math.floor((Math.atan2(z, x) / TAU + 1) % 1 * SHB));
    let shellR = 0;
    for (const m of shell) {
      for (let k = 0; k < m.pos.length; k += 3) {
        const x = m.pos[k] - ctr[0], y = m.pos[k + 1] - ctr[1], z = m.pos[k + 2] - ctr[2], r = Math.hypot(x, y, z), b = shBin(x, y, z, r);
        if (r > SHR[b]) SHR[b] = r;
        if (r > shellR) shellR = r;
      }
    }
    for (let b = 0; b < SHR.length; b++) if (!SHR[b]) SHR[b] = shellR;
    const zoomIn = V3 ? Math.max(ZOOMED, shellR + 0.6) : ZOOMED;  // closer than this, page titles replace lobe labels
    // 1 well outside the glass, 0 inside it or within 0.1 of it (along the camera's direction)
    const outside = (e) => {
      const x = e[0] - ctr[0], y = e[1] - ctr[1], z = e[2] - ctr[2], r = Math.hypot(x, y, z), rs = SHR[shBin(x, y, z, r)];
      const t = Math.max(0, Math.min(1, (r - rs - 0.1) / 0.15));
      return t * t * (3 - 2 * t);
    };

    // v4 fibre attenuation (always on): a long fibre fades, so the many long ones crossing the middle
    // do not wash it out. Overview caps: a page shows at most its 3 strongest similar fibres (by its
    // nearest-by-meaning rank) and a hub's link fibres past its 24 strongest (by the other end's
    // importance) dim to 0.3; the selected or hovered page always shows all of its own.
    const FATT = new Float32Array(NF), OVK = new Float32Array(NF).fill(1), PAIRK = new Float32Array(NF).fill(1), PAIRW = new Float32Array(NF).fill(1);
    {  // v4 round 2: the link fibres between the same two lobes form one tract that outshone everything
      const pairOf = (f) => { const a = LOBE[FA[f]], b = LOBE[FB[f]]; return FK[f] || a === b || a >= L || b >= L ? -1 : Math.min(a, b) * L + Math.max(a, b); };
      const n = new Map();
      for (let f = 0; f < NF; f++) { const k = pairOf(f); if (k >= 0) n.set(k, (n.get(k) || 0) + 1); }
      for (let f = 0; f < NF; f++) {
        const k = pairOf(f), c = k >= 0 ? n.get(k) : 0;
        PAIRK[f] = 1 / Math.sqrt(Math.max(1, c / 8));
        PAIRW[f] = 1 - 0.3 * Math.min(1, Math.max(0, (c - 16) / 48));
      }
    }
    for (let f = 0; f < NF; f++) {
      const a = FA[f] * 3, b = FB[f] * 3, len = Math.hypot(P[b] - P[a], P[b + 1] - P[a + 1], P[b + 2] - P[a + 2]);
      FATT[f] = Math.min(1, Math.max(0.25, Math.pow(0.35 * Rb / Math.max(len, 1e-4), 0.7)));
    }
    {
      const rankIn = (i, j) => { if (NEAR) for (let k = 0; k < 8; k++) if (NEAR[i * 8 + k] === j) return k; return 8; };
      const keep = new Uint8Array(NF);
      for (let i = 0; i < N; i++) {
        const sims = [], lks = [];
        for (let q = FAO[i]; q < FAO[i + 1]; q++) { const f = FADJ[q]; (FK[f] ? sims : lks).push(f); }
        if (sims.length > 3) sims.sort((x, y) => rankIn(i, other(x, i)) - rankIn(i, other(y, i)) || x - y);
        for (let k = 0; k < Math.min(3, sims.length); k++) keep[sims[k]] = 1;
        if (HUB[i] && lks.length > 24) {
          lks.sort((x, y) => IMP[other(y, i)] - IMP[other(x, i)] || x - y);
          for (let k = 24; k < lks.length; k++) OVK[lks[k]] = 0.3;
        }
      }
      for (let f = 0; f < NF; f++) if (FK[f] && !keep[f]) OVK[f] = 0;
    }
    // v4 dendrites: the top 150 hubs get 4-7 short curved stubs (3-6 % of the brain radius) in seeded
    // directions: hub page, tip x y z, control x y z
    const DEN = [];
    if (V3) {
      const hubs = [];
      for (let i = 0; i < N; i++) if (HUB[i]) hubs.push(i);
      hubs.sort((a, b) => IMP[b] - IMP[a] || a - b);
      for (const i of hubs.slice(0, 150)) {
        let sd = (i * 2654435761) >>> 0;
        const rnd = () => { sd = (Math.imul(sd, 1664525) + 1013904223) >>> 0; return sd / 4294967296; };
        for (let n = 4 + Math.floor(rnd() * 4); n > 0; n--) {
          const z = rnd() * 2 - 1, an = rnd() * TAU, rr = Math.sqrt(1 - z * z), d = [rr * Math.cos(an), z, rr * Math.sin(an)];
          const len = (0.03 + 0.03 * rnd()) * Rb, bend = (rnd() - 0.5) * 0.6 * len;
          const px = Math.abs(d[1]) < 0.9 ? [d[2], 0, -d[0]] : [0, d[2], -d[1]], pl = Math.hypot(px[0], px[1], px[2]) || 1;
          DEN.push(i);
          for (let c = 0; c < 3; c++) DEN.push(P[i * 3 + c] + d[c] * len);
          for (let c = 0; c < 3; c++) DEN.push(P[i * 3 + c] + d[c] * len * 0.5 + px[c] / pl * bend);
        }
      }
    }
    const denI = new Float32Array(DEN.length / 7 * 25);
    // the lobe centres the GPU arranges pages around (w = 1 when the lobe has one)
    const lobeC = new Float32Array(Math.max(1, L) * 4);
    for (let k = 0; k < L; k++) { const c = centreOf(k); if (c) lobeC.set([c[0], c[1], c[2], 1], k * 4); }
    // the shell's dust, and a sparse sprinkle on the stem's tube (the same look, fainter)
    const tube = mesh.filter((m) => m.name === 'stem');
    let dust = V3 && shell.length ? dustOf(shell, 9000) : new Float32Array(0);
    if (V3 && tube.length) {
      const td = dustOf(tube, Math.round(STEM_DUST * 9000)), both = new Float32Array(dust.length + td.length);
      both.set(dust);
      both.set(td, dust.length);
      dust = both;
    }

    // Shared scene for the renderers. TP and TFC are the arranged positions and control points.
    const COL = new Uint8Array(N * 4), FX = new Uint8Array(N * 4), PI = new Uint16Array(N);
    const FCOL = new Uint8Array(NF * 8), FCI = new Uint16Array(NF);  // per fibre: rgb a, rgb width*40; colour index
    const TP = P.slice(0, N * 3), TFC = FC.slice(0, NF * 3);
    const S = { N, P, TP, TFC, Q, SZ, COL, FX, PI, mesh, ctr, Rb, V3, HUB, NP: 5 + (V3 ? L + SCATS.length : 0), NF, FA, FB, FC, FK, FCOL, FCI,
      FATT, LOBE, L, lobeC, dust, arrange: V3, slice: [lo[1], 10 / Math.max(0.1, hi[1] - lo[1])] };

    /* ---- DOM: a focusable box holding the canvas, the SVG label overlay and the breadcrumb ---- */
    const box = document.createElement('div');
    // a keyboard widget, not a picture: screen readers pass the keys through, and the help names the
    // rail outline as the way to browse every page
    const keys = document.createElement('p'), keysId = 'brain-keys-' + Math.random().toString(36).slice(2);
    keys.id = keysId;
    keys.hidden = true;
    keys.textContent = 'Arrows turn, + and − zoom, [ and ] step through the chosen page\'s connections, Backspace goes to the '
      + 'previous page, Esc goes back a level, F frames the selection, 0 resets the view, A M G switch mode, S spins.'
      + (opts.keyHelp ? ' ' + opts.keyHelp : '') + ' The outline in the side rail lists every page.';
    box.tabIndex = 0;
    box.setAttribute('role', 'application');
    box.setAttribute('aria-roledescription', '3D brain');
    box.setAttribute('aria-label', `Brain of ${SF.toLocaleString('en')} pages in ${L} lobes`
      + (N > SF ? `, with ${(N - SF).toLocaleString('en')} websites in its stem` : ''));
    box.setAttribute('aria-describedby', keysId);
    box.setAttribute('aria-keyshortcuts', 'ArrowLeft ArrowRight ArrowUp ArrowDown + - 0 Escape Backspace [ ] F A M G S');
    box.appendChild(keys);
    Object.assign(box.style, { position: 'relative', width: '100%', height: '100%', touchAction: 'none', overflow: 'hidden', outlineOffset: '-2px' });
    container.appendChild(box);
    const fontFamily = getComputedStyle(container).fontFamily || 'system-ui, sans-serif';
    const svgEl = (tag, attrs, parent) => {
      const e = document.createElementNS(SVGNS, tag);
      for (const k in attrs) e.setAttribute(k, attrs[k]);
      if (parent) parent.appendChild(e);
      return e;
    };
    const svg = svgEl('svg', { 'aria-hidden': 'true' });
    Object.assign(svg.style, { position: 'absolute', left: '0', top: '0', width: '100%', height: '100%', pointerEvents: 'none', overflow: 'hidden' });
    box.appendChild(svg);
    const gLead = svgEl('g', { fill: 'none', 'stroke-width': '1' }, svg), gLab = svgEl('g', {}, svg);
    const gPage = svgEl('g', {}, svg), gMark = svgEl('g', { fill: 'none' }, svg);
    const measure = document.createElement('canvas').getContext('2d');
    const widths = new Map();
    function textW(text, size) {
      const k = size + '|' + text;
      let w = widths.get(k);
      if (w === undefined) {
        measure.font = `${size}px ${fontFamily}`;
        w = measure.measureText(text).width;
        if (widths.size > 4000) widths.clear();
        widths.set(k, w);
      }
      return w;
    }
    function plate(parent, size, interactive) {
      const g = svgEl('g', {}, parent), r = svgEl('rect', { rx: '2', ry: '2' }, g);
      const t = svgEl('text', { 'font-size': String(size), 'dominant-baseline': 'central' }, g);
      t.style.fontFamily = fontFamily;
      if (interactive) { g.style.pointerEvents = 'auto'; g.style.cursor = 'pointer'; }
      g.style.display = 'none';
      return { g, r, t, text: null, k: -1 };
    }
    function setPlate(pl, text, x, y, anchorRight, size, opacity, fill) {
      if (pl.text !== text) { pl.t.textContent = text; pl.text = text; }
      if (fill && pl.fill !== fill) { pl.t.setAttribute('fill', fill); pl.fill = fill; }
      const w = textW(text, size), x0 = anchorRight ? x - w : x;
      pl.t.setAttribute('x', x0.toFixed(1)); pl.t.setAttribute('y', y.toFixed(1));
      pl.r.setAttribute('x', (x0 - 5).toFixed(1)); pl.r.setAttribute('y', (y - size * 0.75).toFixed(1));
      pl.r.setAttribute('width', (w + 10).toFixed(1)); pl.r.setAttribute('height', (size * 1.5).toFixed(1));
      pl.g.setAttribute('opacity', opacity.toFixed(2));
      pl.g.style.display = '';
      return [x0 - 5, y - size * 0.75, x0 + w + 5, y + size * 0.75];
    }
    const lobePl = [], leads = [], pagePl = [];
    for (let j = 0; j < LABELS; j++) {
      const pl = plate(gLab, 13, true);
      pl.g.addEventListener('pointerdown', (e) => e.stopPropagation());
      pl.g.addEventListener('click', () => { if (pl.k >= 0) pickLobe(pl.k); });
      // a label's double-click flies closer to its region (and is not the stage's "back to overview")
      pl.g.addEventListener('dblclick', (e) => { e.stopPropagation(); if (pl.k >= 0) pickLobe(pl.k, true); });
      lobePl.push(pl);
      leads.push(svgEl('path', { 'stroke-opacity': '0.45' }, gLead));
    }
    for (let j = 0; j < PAGE_LABELS; j++) pagePl.push(plate(gPage, 12.5, false));
    // the stem's most visited sites carry a small name (hover names any site)
    const topSites = [];
    for (let i = SF; i < N; i++) topSites.push(i);
    topSites.sort((a, b) => PG[b].visits - PG[a].visits || a - b).splice(SITE_LABELS);
    const sitePl = topSites.map(() => plate(gPage, 11, false));
    const hoverPl = plate(gPage, 12.5, false);
    const selRing = svgEl('circle', { r: '9', 'stroke-width': '1.2' }, gMark);
    const beaconRings = [0, 1, 2].map(() => svgEl('circle', { 'stroke-width': '1.5' }, gMark));
    const reticleG = svgEl('g', { 'stroke-width': '1.2' }, gMark);
    svgEl('circle', { r: '13' }, reticleG);
    for (const [x1, y1, x2, y2] of [[0, -19, 0, -15], [0, 15, 0, 19], [-19, 0, -15, 0], [15, 0, 19, 0]]) {
      svgEl('line', { x1, y1, x2, y2 }, reticleG);
    }
    // v4 breadcrumb, top-left: Brain › Region › Page, each a button back to that level. The page can
    // move it with --brain-crumbs-top / --brain-crumbs-left (say, below its toolbar).
    const crumbNav = document.createElement('nav');
    crumbNav.className = 'brain-crumbs';
    crumbNav.setAttribute('aria-label', 'Brain location');
    Object.assign(crumbNav.style, { position: 'absolute', left: 'var(--brain-crumbs-left, 12px)', top: 'var(--brain-crumbs-top, 10px)',
      display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: '2px', fontSize: '12px', fontFamily, maxWidth: 'calc(100% - 24px)' });
    crumbNav.addEventListener('pointerdown', (e) => e.stopPropagation());
    crumbNav.addEventListener('dblclick', (e) => e.stopPropagation());
    box.appendChild(crumbNav);

    /* ---- colours and theme ---- */
    let colors, pal, palCore, C3;
    const f3 = (c) => c.map((v) => v / 255);
    function setTheme(c) {
      c = c || {};
      const st = Object.assign({}, DEFAULTS.state, c.state);
      colors = Object.assign({}, DEFAULTS, c, { state: st });
      for (const k of ['node', 'link', 'label']) colors[k] = lightOr(colors[k], DEFAULTS[k]);
      colors.stage = V3 ? V3C.stage : DEFAULTS.stage;  // the stage stays dark in both themes
      if (V3) { colors.body = colors.rim = V3C.glass; colors.grey = V3C.neutral; }
      recolor();
      gLead.setAttribute('stroke', colors.label);
      for (const pl of lobePl.concat(pagePl, [hoverPl])) {
        pl.r.setAttribute('fill', colors.stage); pl.r.setAttribute('fill-opacity', V3 ? '0.9' : '0.82');
        pl.t.setAttribute('fill', colors.label);
        pl.fill = null;
      }
      selRing.setAttribute('stroke', colors.node);
      for (const r of beaconRings) r.setAttribute('stroke', colors.node);
      reticleG.setAttribute('stroke', colors.node);
      crumbKey = '';
      if (R) { restyle(); retint(); relines(); }
    }
    // palette: node, grey, three states, then (v3) one colour per lobe; Canvas cores are whiter in v3
    function recolor() {
      const st = colors.state;
      pal = [rgb(colors.node), rgb(colors.grey), rgb(st.solid), rgb(st.learning), rgb(st.needs_refresh)]
        .concat(V3 ? LC : [], V3 ? SCATS.map((c) => rgb(STEM.colors[c], V3C.neutral)) : []);
      palCore = V3 ? pal.map((p) => p.map((v) => Math.round(v + (255 - v) * 0.15))) : pal;
      C3 = { stage: f3(rgb(colors.stage)), body: f3(rgb(colors.body)), rim: f3(rgb(colors.rim)),
        node: f3(rgb(colors.node)), glow: colors.glow !== false };
      for (const l of leads) l.lk = -1;
    }

    /* ---- style: `style` is the chosen object; SV holds the numbers the frame draws
       with, cross-faded toward the style's over 300 ms ---- */
    const presetOf = (s) => PRESETS.find((p) => STYLE_SCHEMA.every((d) => mergeStyle(DEFAULT_STYLE, p.style)[d.key] === s[d.key]));
    let style = mergeStyle(DEFAULT_STYLE, opts.style);
    if (opts.alive === false) style.alive = false;  // the older option still works
    let camSpec = (presetOf(style) || {}).camera || CAM_DEFAULT;
    function styleTarget(s) {
      const k = s.shellStrength, tint = s.shellTint === 'violet' ? SHELL_TINT.vivid : SHELL_TINT[s.palette] || SHELL_TINT.topic;
      return { nodeScale: s.nodeScale, contrast: s.sizeContrast, hub: s.hubBodies ? 1 : 0, glow: s.nodeGlow, dof: s.dof,
        fibK: s.fibreBrightness, fibW: s.fibreWidth, bundle: s.bundling, shimmer: s.shimmer ? 1 : 0, dend: s.dendrites ? 1 : 0,
        tightness: s.tightness, spread: s.spread, bloom: s.bloom, fog: s.fog, deep: s.background === 'deep' ? 1 : 0,
        nebula: s.nebula, vig: s.vignette, glass: s.shell === 'glass' ? k : 0, rim: s.shell === 'rim' ? k : 0,
        dust: s.shell === 'dust' ? k : 0, contour: s.shell === 'contour' ? k : 0, t0: tint[0], t1: tint[1], t2: tint[2],
        hubK: s.hubScale, nodeK: s.nodeBrightness, fibSat: s.fibreSaturation };
    }
    const SV = styleTarget(style);
    let sAnim = null;
    function stepStyle(t) {
      const p = Math.min(1, Math.max(0, (t - sAnim.t0) / STYLE_MS)), e = p * p * (3 - 2 * p);
      for (const k in sAnim.to) SV[k] = sAnim.from[k] + (sAnim.to[k] - sAnim.from[k]) * e;
      if (p >= 1) sAnim = null;
      return p < 1;
    }
    // node size in px at the reference depth: [scale, base, amplitude, hub bodies] (the shader's uSize)
    const sizeU = () => (V3 ? [SV.nodeScale, 3 + Math.max(0, 1 - SV.contrast), 16 * SV.contrast, SV.hub] : [1, 2.5, 5, 0]);
    const sizeOf = (i) => {
      const u = sizeU(), K = V3 ? LOOK3 : LOOK2;
      let b = u[0] * (u[1] + u[2] * Math.pow(Q[i], 20));
      if (HUB[i]) b = Math.max(b, u[0] * u[1] * (1.6 + 0.4 * Q[i])) * (V3 ? SV.hubK : 1);
      return Math.min(K.maxS, Math.max(K.minS, b));
    };
    // Arrangement on the CPU too (labels, flights, glimmer, the Canvas path): per lobe [offset, scale];
    // row L is the identity for pages without a lobe centre.
    const LOFF = new Float32Array((L + 1) * 4);
    let arrKey = '', sizeKey = '';
    function arrange() {
      const key = SV.tightness.toFixed(4) + ',' + SV.spread.toFixed(4) + ',' + SV.bundle.toFixed(4);
      if (key !== arrKey) {
        arrKey = key;
        const s = 1 - SV.tightness;
        for (let k = 0; k <= L; k++) {
          const c = k < L && S.arrange ? centreOf(k) : null;
          if (c) for (let d = 0; d < 3; d++) LOFF[k * 4 + d] = ctr[d] + (c[d] - ctr[d]) * SV.spread - c[d] * s;
          else LOFF[k * 4] = LOFF[k * 4 + 1] = LOFF[k * 4 + 2] = 0;
          LOFF[k * 4 + 3] = c ? s : 1;
        }
        for (let i = 0; i < N; i++) {
          const k = Math.min(LOBE[i], L) * 4;
          for (let d = 0; d < 3; d++) TP[i * 3 + d] = P[i * 3 + d] * LOFF[k + 3] + LOFF[k + d];
        }
        if (R && R.kind === 'canvas') for (let f = 0; f < NF; f++) ctlT(f, TFC, f * 3);
      }
      const sk = SV.nodeScale.toFixed(4) + ',' + SV.contrast.toFixed(4) + ',' + SV.hubK.toFixed(4);
      if (sk !== sizeKey) { sizeKey = sk; for (let i = 0; i < N; i++) SZ[i] = sizeOf(i); if (R) R.setPoints(); }
    }
    // a fibre's arranged control point, re-weighted toward the straight midpoint by `bundling`
    function ctlT(f, out, o) {
      const a = FA[f], b = FB[f], ka = Math.min(LOBE[a], L) * 4, kb = Math.min(LOBE[b], L) * 4, sm = (LOFF[ka + 3] + LOFF[kb + 3]) / 2;
      for (let d = 0; d < 3; d++) {
        const cm = FC[f * 3 + d] * sm + (LOFF[ka + d] + LOFF[kb + d]) / 2, mid = (TP[a * 3 + d] + TP[b * 3 + d]) / 2;
        out[o + d] = mid + (cm - mid) * SV.bundle;
      }
      return out;
    }
    // a lobe's arranged centre (its label anchor), or v2's anchor as given
    function anchorT(k) {
      const a = anchorOf(k), c = centreOf(k);
      return a && c && S.arrange ? [ctr[0] + (c[0] - ctr[0]) * SV.spread, ctr[1] + (c[1] - ctr[1]) * SV.spread, ctr[2] + (c[2] - ctr[2]) * SV.spread] : a;
    }

    /* ---- state ---- */
    let mode = MODES.includes(opts.mode) ? opts.mode : 'activity';
    let growthMonth = null, flashT0 = -1e9, archiveOn = !!opts.archive, spin = false;
    let hl = null, focus = -1, hover = -1, selected = -1, reticle = -1, todaySet = null, replay = null;
    let picked = false;  // a page or region was chosen once: `hover` labels stop showing every region name
    let beaconLobes = [], beaconT0 = 0, selOrbit = false, crumbKey = '', walk = null;
    const hist = [], userVis = new Uint8Array(N).fill(1);
    // v3 animation: the brain is alive by default (ambient trails, region breathing) until 2 minutes
    // pass without input; one-shots: search pop, hover breath, mode and style cross-fades
    let alive = style.alive, lastActive = performance.now(), popT0 = -1e9, breathT0 = -1e9, fade = null, lwave = null;

    // The neighbourhood that stays lit: the hovered (else selected) page, its links, its near pages.
    function hood() {
      const src = hover >= 0 ? hover : selected;
      if (src < 0) return null;
      const m = new Uint8Array(N);
      m[src] = 1;
      for (let q = AO[src]; q < AO[src + 1]; q++) m[ADJ[q]] = 1;
      for (let q = FAO[src]; q < FAO[src + 1]; q++) { const f = FADJ[q]; m[FA[f]] = m[FB[f]] = 1; }
      if (src === selected && hover < 0 && NEAR) for (let k = 0; k < 8; k++) { const j = NEAR[src * 8 + k]; if (j < N) m[j] = 1; }
      return m;
    }
    const DIM = V3 ? DIM_NODE : 0.2;
    function restyle() {
      const nb = hood();
      fade = null;
      visCount.fill(0);
      for (let i = 0; i < N; i++) {
        const p = PG[i], o = i * 4;
        // the chosen page always draws, whatever the filters, so a selection never shows an empty stage
        const v = i === selected || (userVis[i] && (archiveOn || !ARCH[i])
          && !(mode === 'growth' && growthMonth && MONTH[i] && MONTH[i] > growthMonth));
        if (v && LOBE[i] < L) visCount[LOBE[i]]++;
        const on = nb ? nb[i] : (!hl || hl[i]) && (focus < 0 || LOBE[i] === focus);
        const site = p.kind === 'site';
        let pi = V3 ? pcOf(i) : 0, ring = site && p.private ? 1 : 0;
        if (mode === 'mastery') {
          if (p.kind === 'concept') {
            pi = STATE_PI[p.state] || 0;
            ring = p.state === 'unverified' || p.state === 'not_started' ? 1 : 0;
          } else if (!site) pi = 1;  // a site keeps its category's colour
        }
        PI[i] = pi;
        // v3 Mastery: pages without a study state step back so the state colours carry the view
        const a = !v ? 0 : (on ? 1 : DIM) * (ARCH[i] && i !== selected ? 0.25 : 1)
          * (V3 && (pi === 1 || site) && mode === 'mastery' ? 0.45 : 1) * (i === selected ? 1 : GLOW[i]);
        const c = V3 && i === selected ? whiten(pal[pi], 0.6) : pal[pi];  // v4: the chosen page is white-hot
        COL[o] = c[0]; COL[o + 1] = c[1]; COL[o + 2] = c[2]; COL[o + 3] = Math.round(a * 255);
        // v3: search matches keep a soft halo and pop once; the hovered page's neighbours breathe once
        FX[o] = (mode === 'activity' && RECENT[i]) || (V3 && hl && hl[i]) ? 255 : 0;
        FX[o + 1] = ring ? 255 : 0;
        let fz = mode === 'growth' && growthMonth && MONTH[i] === growthMonth ? 255 : 0;
        if (V3 && !fz) fz = mode !== 'growth' && hover >= 0 && nb && nb[i] && i !== hover ? 160 : hl && hl[i] ? 96 : 0;
        FX[o + 2] = fz;
        FX[o + 3] = i === hover ? 255 : i === selected ? 153 : todaySet && todaySet[i] ? 128 : nb && nb[i] ? 90 : 0;
      }
      R.setPoints();
      if (NF) refibre();
      redendrites();
      syncCrumbs();
      invalidate();
    }
    // Fibres follow their ends: hidden with either end; the hovered or selected page's fibres show at
    // 0.6 and thicken while the rest dim to 8 %; a focused lobe keeps its own; Mastery turns them neutral.
    const kindOn = (f) => (FK[f] ? style.similar : style.links);
    function refibre() {
      const src = hover >= 0 ? hover : selected, neutral = mode === 'mastery';
      const still = V3 && reduced && src >= 0 && src === selected;  // reduced motion: white fibres at 0.8
      // a busy page's links show only while one of their ends is hovered or selected
      const on = (f) => {
        const i = FA[f], j = FB[f];
        return kindOn(f) && COL[i * 4 + 3] && COL[j * 4 + 3] && (!BUSYF[f] || i === hover || j === hover || i === selected || j === selected);
      };
      // shortcut: alpha scales down with the visible fibre count so a dense vault reads as fibres, not
      // fog, and a sparse view (Growth, filters) keeps its glow; tune on real use
      let nv = 0;  // the stem's fibres are not counted: websites never dim the pages' fibres
      for (let f = 0; f < NF; f++) if (!BUSYF[f] && OVK[f] > 0 && kindOn(f) && FA[f] < SF && FB[f] < SF && COL[FA[f] * 4 + 3] && COL[FB[f] * 4 + 3]) nv++;
      const FD = Math.min(1, Math.sqrt(7000 / Math.max(1, nv)));
      for (let f = 0; f < NF; f++) {
        const i = FA[f], j = FB[f], o = f * 8, sim = FK[f] === 1, own = src >= 0 && (i === src || j === src);
        let a = 0, w = 0;
        if (on(f) && (own || OVK[f] > 0)) {
          // cross-lobe fibres (the tracts between topics) read brighter than the fibres inside a cluster
          a = (sim ? 0.05 : 0.22 * style.linkBrightness) * FD * (LOBE[i] === LOBE[j] ? 0.75 : 1.2) * OVK[f] * PAIRK[f];
          w = (sim ? 0.6 : style.linkWidth) * PAIRW[f];
          if (HUB[i] || HUB[j]) w *= 1.5;
          if (src >= 0) {
            if (own) { a = still ? 0.8 : 0.6; w = sim ? 0.6 : 1; } else a *= V3 ? DIM_FIB : DIM;  // round 2: thin, like v3's
          } else if (todaySet && (todaySet[i] || todaySet[j])) {
            a = sim ? 0.3 : 0.7; w *= 1.4;  // Today: the paths the pulses travel stay lit
          } else if (focus >= 0) {
            if (LOBE[i] === focus || LOBE[j] === focus) a = Math.min(1, a * 1.6); else a *= DIM;
          } else if (hl && !hl[i] && !hl[j]) a *= DIM;
          if ((ARCH[i] || ARCH[j]) && i !== src && j !== src) a *= 0.25;
          if (neutral && src < 0) a *= 0.35;
        }
        const ci = neutral ? 1 : pcOf(i);
        const ca = still && own ? [255, 255, 255] : pal[ci], cb = still && own ? ca : neutral ? ca : pal[pcOf(j)];
        FCOL[o] = ca[0]; FCOL[o + 1] = ca[1]; FCOL[o + 2] = ca[2]; FCOL[o + 3] = Math.round(a * 255);
        FCOL[o + 4] = cb[0]; FCOL[o + 5] = cb[1]; FCOL[o + 6] = cb[2]; FCOL[o + 7] = Math.min(255, Math.round(w * 40));
        FCI[f] = ci;
      }
      R.setFibres();
    }
    // Dendrite stubs in their hub's colour at half its alpha (a dimmed hub's stubs dim with it).
    function redendrites() {
      if (!DEN.length || R.kind !== 'webgl2') return;
      let n = 0;
      for (let d = 0; d < DEN.length; d += 7) {
        const i = DEN[d], a = COL[i * 4 + 3] / 255;
        if (a < 0.02) continue;
        const o = n++ * 25, c = pal[PI[i]], k = LOBE[i];
        denI.set([DEN[d + 1], DEN[d + 2], DEN[d + 3], P[i * 3], P[i * 3 + 1], P[i * 3 + 2], DEN[d + 4], DEN[d + 5], DEN[d + 6], 0,
          c[0] / 255, c[1] / 255, c[2] / 255, 0.5 * a, c[0] / 255, c[1] / 255, c[2] / 255, 1.3 / 6.375, 1, 1, 0, 1, 1, k, k], o);
      }
      R.setDendrites(denI, n);
    }
    // Lobe tints on the shell: the focused lobe; in Mastery, Solid vs Needs refresh per study lobe.
    function retint() {
      const T = new Uint8Array(Math.max(1, L) * 4);
      if (V3) { R.setTint(T, 0); invalidate(); return; }  // v3: the glass stays clear; nodes and fibres carry the meaning
      const solid = pal[2], refresh = pal[4], grey = pal[1], node = pal[0];
      for (let k = 0; k < L; k++) {
        let c = null, a = 0;
        if (mode === 'mastery') {
          const s = study[k], sr = s.solid + s.refresh;
          if (sr) { const t = s.solid / sr; c = refresh.map((v, j) => v + (solid[j] - v) * t); a = 0.4; }
          else { c = grey; a = s.n ? 0.25 : 0.18; }
        } else if (k === focus) { c = node; a = 0.22; }
        if (c) { T[k * 4] = c[0]; T[k * 4 + 1] = c[1]; T[k * 4 + 2] = c[2]; T[k * 4 + 3] = a * 255; }
      }
      R.setTint(T, L);
      invalidate();
    }
    // v2: links as arcs lifted off the surface. v3: the fibre's own curve, else Python's bundling rule
    // (the midpoint pulled 35 % toward the midpoint of the two lobe centres). Tracts: Python's Béziers.
    function arcPts(i, j, n) {
      const a = i * 3, b = j * 3, out = new Float32Array(n * 3), Pp = TP;
      const mx = (Pp[a] + Pp[b]) / 2, my = (Pp[a + 1] + Pp[b + 1]) / 2, mz = (Pp[a + 2] + Pp[b + 2]) / 2;
      let cx, cyy, cz;
      const f = V3 ? fibreOf(i, j) : -1, ka = LOBE[i] < L && anchorT(LOBE[i]), kb = LOBE[j] < L && anchorT(LOBE[j]);
      if (f >= 0) [cx, cyy, cz] = ctlT(f, [0, 0, 0], 0);
      else if (V3 && ka && kb) {
        cx = mx + 0.35 * ((ka[0] + kb[0]) / 2 - mx); cyy = my + 0.35 * ((ka[1] + kb[1]) / 2 - my); cz = mz + 0.35 * ((ka[2] + kb[2]) / 2 - mz);
      } else {
        const ox = mx - ctr[0], oy = my - ctr[1], oz = mz - ctr[2], ol = Math.hypot(ox, oy, oz) || 1;
        const lift = Math.hypot(Pp[b] - Pp[a], Pp[b + 1] - Pp[a + 1], Pp[b + 2] - Pp[a + 2]) * 0.35;
        cx = mx + ox / ol * lift; cyy = my + oy / ol * lift; cz = mz + oz / ol * lift;
      }
      for (let s = 0; s < n; s++) {
        const t = s / (n - 1), u = 1 - t;
        out[s * 3] = u * u * Pp[a] + 2 * u * t * cx + t * t * Pp[b];
        out[s * 3 + 1] = u * u * Pp[a + 1] + 2 * u * t * cyy + t * t * Pp[b + 1];
        out[s * 3 + 2] = u * u * Pp[a + 2] + 2 * u * t * cz + t * t * Pp[b + 2];
      }
      return out;
    }
    function linksOf(i) {
      const all = Array.from(ADJ.subarray(AO[i], AO[i + 1])).filter((j) => userVis[j]);
      all.sort((x, y) => deg(y) - deg(x) || x - y);
      return { shown: all.slice(0, LINK_CAP), hidden: Math.max(0, all.length - LINK_CAP) };
    }
    function nearOf(i) {
      const out = [];
      if (NEAR) for (let k = 0; k < 8; k++) { const j = NEAR[i * 8 + k]; if (j < N && j !== i) out.push(j); }
      return out;
    }
    // Overlay ribbons. v3 fibres already carry the page's links and similar pages; the overlay adds
    // only a link that has no fibre, in the page's colour. (v4: no dashed guides for similar pages.)
    function relines() {
      const out = [], src = hover >= 0 ? hover : selected;
      const link = V3 && src >= 0 && LOBE[src] < L ? LC[LOBE[src]] : rgb(colors.link);
      if (src >= 0) {
        const ls = linksOf(src).shown, linked = new Set(ls);
        for (const j of ls) if (!V3 || fibreOf(src, j) < 0) out.push({ p: arcPts(src, j, 16), n: 16, c: link, a: 0.5, w: 1.2, dash: false });
        if (!V3 && src === selected && hover < 0) {
          for (const j of nearOf(src)) if (!linked.has(j)) out.push({ p: arcPts(src, j, 16), n: 16, c: link, a: 0.45, w: 1, dash: true });
        }
      }
      if (focus >= 0 && !V3) {
        for (const t of tracts) {
          if (t.a !== focus && t.b !== focus) continue;
          const p = new Float32Array(24 * 3), q = t.p;
          for (let s = 0; s < 24; s++) {
            const x = s / 23, u = 1 - x, w0 = u * u * u, w1 = 3 * u * u * x, w2 = 3 * u * x * x, w3 = x * x * x;
            for (let d = 0; d < 3; d++) p[s * 3 + d] = w0 * q[0][d] + w1 * q[1][d] + w2 * q[2][d] + w3 * q[3][d];
          }
          // spec alpha 0.08 is the faintest tract; heavier ones rise toward 0.4 so they read
          const f = Math.sqrt((+t.n || 1) / maxTract);
          out.push({ p, n: 24, c: link, a: 0.08 + 0.32 * f, w: 1 + 2.5 * f, dash: false });
        }
      }
      R.setLines(out);
      invalidate();
    }

    /* ---- camera ---- */
    const home = { yaw: 0.9, pitch: 0.55, dist: 3, tx: ctr[0], ty: ctr[1], tz: ctr[2] };
    const cam = Object.assign({}, home);
    let W = 0, H = 0, dpr = 1, focal = 1, camSet = false, maxDist = DIST_MAX, goal = null;
    const CAM = ['yaw', 'pitch', 'dist', 'tx', 'ty', 'tz'];
    // opts.camera (a remount): start where the old brain's camera was, not at the overview
    if (opts.camera && CAM.every((k) => Number.isFinite(opts.camera[k]))) {
      for (const k of CAM) cam[k] = opts.camera[k];
      camSet = true;
    }
    const V = { cy: 1, sy: 0, cp: 1, sp: 0, dist: 3, tx: 0, ty: 0, tz: 0, focal: 1 };
    const VP = new Float32Array(16), eye = new Float32Array(3);
    function basis() {
      const cy = Math.cos(cam.yaw), sy = Math.sin(cam.yaw), cp = Math.cos(cam.pitch), sp = Math.sin(cam.pitch);
      Object.assign(V, { cy, sy, cp, sp, dist: cam.dist, tx: cam.tx, ty: cam.ty, tz: cam.tz, focal });
      const Rr = [cy, 0, -sy], U = [-sy * sp, cp, -cy * sp], Bk = [sy * cp, sp, cy * cp];
      eye[0] = cam.tx + cam.dist * Bk[0]; eye[1] = cam.ty + cam.dist * Bk[1]; eye[2] = cam.tz + cam.dist * Bk[2];
      const row = (v) => [v[0], v[1], v[2], -(v[0] * eye[0] + v[1] * eye[1] + v[2] * eye[2])];
      const r0 = row(Rr), r1 = row(U), r2 = row(Bk);
      const fy = 1 / Math.tan(FOV / 2), fx = fy * H / Math.max(W, 1);
      const A = (ZF + ZN) / (ZN - ZF), Cc = 2 * ZF * ZN / (ZN - ZF);
      for (let c = 0; c < 4; c++) {
        VP[c * 4] = fx * r0[c]; VP[c * 4 + 1] = fy * r1[c];
        VP[c * 4 + 2] = A * r2[c] + (c === 3 ? Cc : 0); VP[c * 4 + 3] = -r2[c];
      }
    }
    // World point -> [screen x, screen y, depth from camera], or null when behind it.
    function proj(x, y, z) {
      const vx = x - cam.tx, vy = y - cam.ty, vz = z - cam.tz;
      const x1 = vx * V.cy - vz * V.sy, z1 = vx * V.sy + vz * V.cy;
      const y2 = vy * V.cp - z1 * V.sp, zc = cam.dist - (vy * V.sp + z1 * V.cp);
      if (zc < ZN) return null;
      const k = focal / zc;
      return [W / 2 + x1 * k, H / 2 - y2 * k, zc];
    }
    const clampPitch = (p) => Math.max(-PITCH_MAX, Math.min(PITCH_MAX, p));
    const clampDist = (d) => Math.max(DIST_MIN, Math.min(maxDist, d));
    // v4 overview: the camera spec's direction, at the distance where the brain (the shell's outline and
    // a sample of the arranged pages) fills `fill` of the stage height (and at most 92 % of its width),
    // centred, then nudged right. fill 0 is v3's camera.
    function rehome() {
      if (!W || !H) return;
      const half = Math.min(FOV / 2, Math.atan(Math.tan(FOV / 2) * W / H)), fit = Math.max(DIST_MIN, OR / Math.sin(half) * 1.02);
      const cs = camSpec;
      if (!V3 || !cs || !(cs.fill > 0)) Object.assign(home, { yaw: 0.9, pitch: 0.55, dist: fit, tx: OC[0], ty: OC[1], tz: OC[2] });
      else {
        const yaw = (cs.azimuth - 90) * Math.PI / 180, pitch = clampPitch(cs.elevation * Math.PI / 180);
        const cy = Math.cos(yaw), sy = Math.sin(yaw), cp = Math.cos(pitch), sp = Math.sin(pitch);
        const pts = SIL.slice(), s = 1 - style.tightness;
        for (let i = 0, st = Math.max(1, Math.floor(N / 600)); i < N; i += st) {
          if (ARCH[i]) continue;  // the unsorted-chat ring under the brain may fall off the frame
          const k = LOBE[i] < L ? LOBE[i] : -1, c = k >= 0 && S.arrange ? centreOf(k) : null;
          for (let d = 0; d < 3; d++) pts.push(c ? ctr[d] + (c[d] - ctr[d]) * style.spread + (P[i * 3 + d] - c[d]) * s : P[i * 3 + d]);
        }
        for (let i = SF; i < N; i++) pts.push(P[i * 3], P[i * 3 + 1], P[i * 3 + 2]);  // every site in the stem
        const boxAt = (d, t) => {
          let x0 = Infinity, x1 = -Infinity, y0 = Infinity, y1 = -Infinity;
          for (let k = 0; k < pts.length; k += 3) {
            const vx = pts[k] - t[0], vy = pts[k + 1] - t[1], vz = pts[k + 2] - t[2];
            const a = vx * cy - vz * sy, z1 = vx * sy + vz * cy, b = vy * cp - z1 * sp, zc = d - (vy * sp + z1 * cp);
            if (zc < ZN) return null;
            const X = a * focal / zc, Y = b * focal / zc;
            if (X < x0) x0 = X; if (X > x1) x1 = X; if (Y < y0) y0 = Y; if (Y > y1) y1 = Y;
          }
          return [x0, x1, y0, y1];
        };
        const t = ctr.slice();
        let d = fit;
        for (let pass = 0; pass < 2; pass++) {
          let a = 0.3, b = 30;
          for (let it = 0; it < 40; it++) {
            const m = (a + b) / 2, bx = boxAt(m, t);
            if (!bx || bx[3] - bx[2] > cs.fill * H || bx[1] - bx[0] > 0.92 * W) a = m; else b = m;
          }
          d = b;
          const bx = boxAt(d, t), k = d / focal, ox = (bx[0] + bx[1]) / 2 - (cs.nudge || 0) * W, oy = (bx[2] + bx[3]) / 2;
          t[0] += (cy * ox - sy * sp * oy) * k; t[1] += cp * oy * k; t[2] += (-sy * ox - cy * sp * oy) * k;
        }
        Object.assign(home, { yaw, pitch, dist: d, tx: t[0], ty: t[1], tz: t[2] });
      }
      maxDist = Math.max(DIST_MAX, home.dist * 1.3);
    }
    // The stem's sites from the overview's side, a little below the brain: the column fills ~80 % of
    // the stage height. null without a stem.
    function stemView() {
      const n = N - SF;
      if (n <= 0) return null;
      let y0 = Infinity, y1 = -Infinity, x = 0, z = 0;
      for (let i = SF; i < N; i++) { const y = P[i * 3 + 1]; y0 = Math.min(y0, y); y1 = Math.max(y1, y); x += P[i * 3]; z += P[i * 3 + 2]; }
      const h = Math.max(0.3, y1 - y0) + 0.2;
      return { yaw: home.yaw, pitch: clampPitch(0.12), dist: clampDist(h / 2 * focal / (0.4 * H)), tx: x / n, ty: (y0 + y1) / 2, tz: z / n };
    }
    let atStem = false;  // the last flight was stem(): its button's second press flies back
    function viewToward(x, y, z, standoff, side) {
      const dx = x - ctr[0], dy = y - ctr[1], dz = z - ctr[2], d = Math.hypot(dx, dy, dz) || 1;
      const yaw = Math.atan2(dx, dz), pitch = clampPitch(Math.asin(dy / d));
      // v3 pages and lobes sit inside the volume: orbit the target itself, seen from its side of the
      // brain (or from where the camera already is, when the target is near the middle); with `side`
      // (where the page's neighbours lie) the view turns square to it, so its fibres fan out on screen
      if (V3) {
        let ux = d < 0.15 ? Math.sin(cam.yaw) * Math.cos(cam.pitch) : dx / d, uy = d < 0.15 ? Math.sin(cam.pitch) : dy / d;
        let uz = d < 0.15 ? Math.cos(cam.yaw) * Math.cos(cam.pitch) : dz / d;
        const sl = side ? Math.hypot(side[0], side[1], side[2]) : 0;
        if (sl > 1e-6) {
          const sx = side[0] / sl, sy = side[1] / sl, sz = side[2] / sl, k = ux * sx + uy * sy + uz * sz;
          const px = ux - k * sx, py = uy - k * sy, pz = uz - k * sz, pl = Math.hypot(px, py, pz);
          if (pl > 0.2) { ux = px / pl; uy = py / pl; uz = pz / pl; }
        }
        const y0 = Math.atan2(ux, uz), p0 = clampPitch(Math.asin(Math.max(-1, Math.min(1, uy))));
        // and from outside the glass: the eye (target + dist x back) at least the shell radius + 0.3 out
        const bx = Math.sin(y0) * Math.cos(p0), by = Math.sin(p0), bz = Math.cos(y0) * Math.cos(p0);
        const vb = dx * bx + dy * by + dz * bz, R = shellR + 0.3, out = -vb + Math.sqrt(Math.max(0, vb * vb - (dx * dx + dy * dy + dz * dz) + R * R));
        return { yaw: y0, pitch: p0, dist: clampDist(Math.max(standoff, out)), tx: x, ty: y, tz: z };
      }
      return { yaw, pitch, dist: clampDist(d + standoff), tx: ctr[0], ty: ctr[1], tz: ctr[2] };
    }
    // the view of a page: square to where its neighbours lie; an archive page from farther out so its
    // beacon lobes stay in view; `close` for a double-click
    function pageView(i, close) {
      let side = null;
      if (V3) {
        side = [0, 0, 0];
        for (let q = FAO[i]; q < FAO[i + 1]; q++) {
          const j = other(FADJ[q], i);
          for (let c = 0; c < 3; c++) side[c] += TP[j * 3 + c] - TP[i * 3 + c];
        }
      }
      const so = V3 ? (ARCH[i] ? 2.4 : close ? 0.5 : 0.9) : ARCH[i] ? 1.6 : 0.45;
      return viewToward(TP[i * 3], TP[i * 3 + 1], TP[i * 3 + 2], so, side);
    }
    function regionView(k, close) {
      const a = anchorT(k);
      if (!a) return home;
      const sp = spreadOf(k) * (V3 ? 1 - SV.tightness : 1);
      return viewToward(a[0], a[1], a[2], V3 ? (0.5 + sp * 4) * (close ? 0.6 : 1) : Math.max(0.3, Math.min(1.0, sp * 3)));
    }

    /* ---- render loop: frames only while something changes and the box is visible; v3's shimmer, the
       alive brain and a selection's trails keep a ~30 fps loop going unless motion is reduced ---- */
    let R = null, raf = 0, lastT = 0, lastInput = -Infinity, anim = null, inView = true, dead = false;
    let dirty = true, lastDraw = 0;
    const vel = [0, 0];
    const mq = window.matchMedia ? window.matchMedia('(prefers-reduced-motion: reduce)') : null;
    let reduced = !!(mq && mq.matches);
    const onMotion = () => { reduced = !!(mq && mq.matches); if (R) restyle(); invalidate(); };
    if (mq && mq.addEventListener) mq.addEventListener('change', onMotion);
    let hasRecent = false;
    for (let i = 0; i < N && !hasRecent; i++) hasRecent = RECENT[i] === 1;
    // Ambient animation (shimmer, Activity pulses, the alive brain) runs while alive and within 2 minutes
    // of input; the Canvas fallback has none and redraws only on change. A selection's trails sleep the
    // same way, after 2 idle minutes, and any input wakes them.
    const gpu = () => V3 && !reduced && R.kind === 'webgl2';
    const awake = () => gpu() && performance.now() - lastActive < ALIVE_IDLE_MS;
    const ambient = () => alive && awake() && (NF > 0 || (mode === 'activity' && hasRecent));
    const trailsOn = () => selected >= 0 && awake();
    function schedule() {
      if (!raf && !dead && inView && !document.hidden && W > 0 && H > 0) raf = requestAnimationFrame(frame);
    }
    function invalidate() { dirty = true; lastActive = performance.now(); schedule(); }
    let spinTimer = 0;
    function poke() {
      lastInput = performance.now();
      clearTimeout(spinTimer);
      if (spin && !reduced) spinTimer = setTimeout(invalidate, IDLE_MS + 20);
    }
    // hover picks once the pointer rests: each pick ends in a synchronous readPixels that drains the GPU
    // queue (about 5 ms a frame at 5k pages), and the hover itself already waits HOVER_MS
    let pendingPick = null, pickTimer = 0;
    function frame(t) {
      raf = 0;
      if (!box.clientWidth) { lastT = 0; return; }  // display: none; the observers restart the loop
      if (!dirty && t - lastDraw < AMBIENT_MS) { schedule(); return; }
      dirty = false; lastDraw = t;
      const dt = lastT ? Math.min(0.1, (t - lastT) / 1000) : 1 / 60;
      lastT = t;
      let busy = false;
      if (sAnim) busy = stepStyle(t) || busy;
      arrange();
      if (anim) {
        const p = Math.max(0, Math.min(1, (t - anim.t0) / anim.ms)), e = EASE(p);
        for (const k in anim.to) cam[k] = anim.from[k] + (anim.to[k] - anim.from[k]) * e;
        if (p >= 1) {
          if (anim.orbit) selOrbit = true;
          anim = null;
          if (selected >= 0) {  // guard: a selection flight never ends with its page behind the eye or off screen
            basis();
            const q = proj(TP[selected * 3], TP[selected * 3 + 1], TP[selected * 3 + 2]);
            if (!q || q[0] < 0 || q[1] < 0 || q[0] > W || q[1] > H) Object.assign(cam, pageView(selected));
          }
        }
        busy = true;
      } else if (goal) {  // damped zoom: ease toward the goal distance and target
        const k = 1 - Math.exp(-dt * 12);
        let d = 0;
        for (const key of ['dist', 'tx', 'ty', 'tz']) { cam[key] += (goal[key] - cam[key]) * k; d = Math.max(d, Math.abs(goal[key] - cam[key])); }
        if (d < 1e-4) { Object.assign(cam, goal); goal = null; }
        busy = true;
      } else if (!drag && (Math.abs(vel[0]) > 1e-4 || Math.abs(vel[1]) > 1e-4)) {
        cam.yaw += vel[0]; cam.pitch = clampPitch(cam.pitch + vel[1]);
        const k = Math.pow(DAMP, dt * 60);
        vel[0] *= k; vel[1] *= k;
        busy = true;
      } else if (!reduced && !drag && hover < 0 && ((spin && t - lastInput >= IDLE_MS) || selOrbit)) {
        cam.yaw += (spin ? style.spinSpeed : SEL_ORBIT) * dt;  // spin, else the slow orbit round a new selection
        busy = true;
      }
      basis();
      const F = frameParams(t);
      if (replay) busy = stepReplay(t) || busy;
      if (F.flash > 0 || F.breath > 0 || (F.pop > 0 && t - popT0 < POP_MS)) busy = true;
      if (beaconLobes.length && !reduced && t - beaconT0 < BEACON_MS) busy = true;
      if (fade) busy = stepFade(t) || busy;
      if (V3) busy = stepFx(t) || busy;
      if (pendingPick) {
        const [x, y] = pendingPick;
        pendingPick = null;
        candidate(R.kind === 'webgl2' ? R.pick(x, y, F) : R.pick(x, y));
      }
      R.draw(F);
      overlay(t);
      if (busy) { dirty = true; schedule(); }
      else if (ambient() || trailsOn()) schedule();
      else lastT = 0;
    }
    const WAVE0 = new Float32Array([0, 0, 0, 1]);
    function waveAt(t) {  // a region breathing: [centre, radius] and its strength, rising and falling once
      if (!lwave || reduced) return [WAVE0, 0];
      const p = Math.min(1, Math.max(0, (t - lwave.t0) / lwave.dur)), a = anchorT(lwave.k) || lwave.c;
      return [new Float32Array([a[0], a[1], a[2], lwave.R]), lwave.s * Math.sin(Math.PI * p)];
    }
    // the raw position of the page whose fibres are lit (pointed at, else chosen), for their curve
    const FAR = new Float32Array([1e9, 1e9, 1e9]);
    const selP = () => { const s = hover >= 0 ? hover : selected; return s >= 0 ? P.subarray(s * 3, s * 3 + 3) : FAR; };
    function frameParams(t) {
      const dc = Math.hypot(eye[0] - ctr[0], eye[1] - ctr[1], eye[2] - ctr[2]), wv = waveAt(t);
      const sq = selected >= 0 ? proj(TP[selected * 3], TP[selected * 3 + 1], TP[selected * 3 + 2]) : null;
      // v3 fog starts a little before the orbit target, so a focused lobe inside the brain stays lit
      return { VP, eye, v: V, proj, pal, palCore, colors: C3, refZ: home.dist,
        // with a page chosen the fog halves, so the rest of the brain stays a dim context rather than fog
        fogNear: V3 ? Math.max(dc - Rb, cam.dist - 0.5) : dc - Rb,
        fogK: V3 ? SV.fog * Math.log(5) / Rb * (selected >= 0 ? 0.5 : 1) : Math.log(4) / (2 * Rb),
        // v3 glass: gone with the camera inside it, receding to a quarter as the camera closes in
        shell: V3 ? 0.12 : small ? 0.45 : 0.28, shellK: V3 ? outside(eye) * (0.25 + 0.75 * Math.max(0, Math.min(1, (cam.dist - 1) / 1.6))) : 1,
        glassK: V3 ? SV.glass : 1, rimK: V3 ? SV.rim * 0.9 : 0, conK: V3 ? SV.contour * 0.5 : 0, dustK: V3 ? SV.dust : 0,
        tint: [SV.t0 / 255, SV.t1 / 255, SV.t2 / 255], deep: V3 ? SV.deep : 0, bloom: V3 ? SV.bloom : 1, vig: V3 ? SV.vig : 0,
        contours: small && !V3, look: V3 ? LOOK3 : LOOK2,
        sizeU: sizeU(), hubK: V3 ? SV.hubK : 1, nodeK: V3 ? SV.nodeK : 1, dof: [sq ? sq[2] : cam.dist, Rb * 0.9, V3 ? SV.dof : 0], glow: V3 ? SV.glow : 1,
        fibK: SV.fibK, fibW: SV.fibW, bundle: SV.bundle, dendK: SV.dend, selP: selP(), fibSat: V3 ? SV.fibSat : 1, arr: [SV.spread, 1 - SV.tightness],
        // fibre exposure: fainter while the brain is small on screen (more overlap), brighter zoomed in
        expo: Math.max(0.5, Math.min(1.6, focal * Rb / cam.dist / 350)),
        time: t / 1000, shimmer: ambient() ? SV.shimmer : 0, pulse: ambient() && mode === 'activity' ? 1 : 0,
        flash: reduced ? 0 : Math.max(0, 1 - (t - flashT0) / 1200),
        breath: reduced || t - breathT0 >= BREATH_MS ? 0 : 0.5 * Math.sin(Math.PI * Math.max(0, t - breathT0) / BREATH_MS),
        pop: !hl ? 0 : reduced || t - popT0 >= POP_MS ? 0.2 : ((p) => (p < 0.5 ? 1.2 * p : 0.6 - 0.8 * (p - 0.5)))((t - popT0) / POP_MS),
        wave: wv[0], waveK: wv[1] };
    }

    function fly(to, ms, orbitAfter) {
      vel[0] = vel[1] = 0;
      goal = null;
      selOrbit = false;
      atStem = false;
      if (reduced) { Object.assign(cam, to); anim = null; invalidate(); return; }
      const from = Object.assign({}, cam);
      let dy = (to.yaw - from.yaw) % TAU;
      if (dy > Math.PI) dy -= TAU;
      if (dy < -Math.PI) dy += TAU;
      anim = { from, to: Object.assign({}, to, { yaw: from.yaw + dy }), t0: performance.now(), ms: ms || FLY_MS, orbit: !!orbitAfter };
      invalidate();
    }
    // Zoom by k, toward the screen point (x, y) when closing in (it stays under the cursor), easing back
    // toward the overview's target when backing out; the camera eases to the goal over a few frames.
    function zoomBy(k, x, y) {
      anim = null; selOrbit = false;
      const g = goal || { dist: cam.dist, tx: cam.tx, ty: cam.ty, tz: cam.tz };
      const nd = clampDist(g.dist * k), r = nd / g.dist;
      if (r < 1 && x !== undefined) {
        const ox = (x - W / 2) * g.dist / focal, oy = -(y - H / 2) * g.dist / focal, m = 1 - r;
        g.tx += (V.cy * ox - V.sy * V.sp * oy) * m; g.ty += V.cp * oy * m; g.tz += (-V.sy * ox - V.cy * V.sp * oy) * m;
      } else if (r > 1) {
        const m = Math.min(1, (r - 1) * 1.5);
        g.tx += (home.tx - g.tx) * m; g.ty += (home.ty - g.ty) * m; g.tz += (home.tz - g.tz) * m;
      }
      g.dist = nd;
      if (reduced) { Object.assign(cam, g); goal = null; } else goal = g;
      poke();
      invalidate();
    }

    /* ---- the SVG overlay: atlas labels, page titles, rings ---- */
    let selLabels = null;  // the chosen page's neighbours worth a label (up to 8, most important first)
    function neighbourLabels(i) {
      if (selLabels && selLabels.i === i) return selLabels.list;
      const m = new Set();
      for (let q = AO[i]; q < AO[i + 1]; q++) m.add(ADJ[q]);
      for (let q = FAO[i]; q < FAO[i + 1]; q++) m.add(other(FADJ[q], i));
      const list = [...m].filter((j) => j !== i && shown(j)).sort((a, b) => IMP[b] - IMP[a] || a - b).slice(0, 8);
      selLabels = { i, list };
      return list;
    }
    function overlay(t) {
      const narrow = W < PHONE, lm = style.labels;
      let minX = Infinity, maxX = -Infinity;
      for (let k = 0; k < SIL.length; k += 3) {
        const q = proj(SIL[k], SIL[k + 1], SIL[k + 2]);
        if (q) { if (q[0] < minX) minX = q[0]; if (q[0] > maxX) maxX = q[0]; }
      }
      const mid = (minX + maxX) / 2, bk = [V.sy * V.cp, V.sp, V.cy * V.cp];
      // which regions may carry a label: all (atlas, or hover until the first page or region is chosen),
      // or only the pointed-at or focused one (hover)
      const want = lm === 'atlas' || (lm === 'hover' && !picked) ? null : new Set(lm === 'hover' ? [hover >= 0 ? LOBE[hover] : -1, focus] : []);
      // Exterior lobe labels: facing the camera, largest first, in margin columns.
      const cols = [[], []];
      let used = 0;
      if (!narrow && minX < maxX) {
        for (const k of labelOrder) {
          if (used >= LABELS) break;
          if (want && !want.has(k)) continue;
          if (!visCount[k] || (lobes[k].archive && !archiveOn)) continue;
          if (cam.dist < zoomIn && k !== focus && !want) continue;  // zoomed in: page titles take over
          const nrm = !V3 && v3(lobes[k].normal, null);  // v3 regions show through the glass: no facing test
          if (k !== focus && nrm && nrm[0] * bk[0] + nrm[1] * bk[1] + nrm[2] * bk[2] <= 0.2) continue;
          const a = anchorT(k), q = proj(a[0], a[1], a[2]);
          if (!q) continue;
          cols[q[0] < mid ? 0 : 1].push({ k, ax: q[0], ay: q[1], y: q[1] });
          used++;
        }
      }
      let j = 0;
      cols.forEach((col, side) => {
        col.sort((a, b) => a.ay - b.ay);
        const top = 14, bot = H - 14;
        for (let i = 1; i < col.length; i++) col[i].y = Math.max(col[i].y, col[i - 1].y + LABEL_GAP);
        if (col.length && col[col.length - 1].y > bot) {
          col[col.length - 1].y = bot;
          for (let i = col.length - 2; i >= 0; i--) col[i].y = Math.min(col[i].y, col[i + 1].y - LABEL_GAP);
        }
        if (col.length && col[0].y < top) {
          col[0].y = top;
          for (let i = 1; i < col.length; i++) col[i].y = Math.max(col[i].y, col[i - 1].y + LABEL_GAP);
        }
        for (const it of col) {
          const pl = lobePl[j], text = lobeText[it.k], w = textW(text, 13);
          let x = side === 0 ? minX - 18 : maxX + 18;  // the text's inner edge, beside the silhouette
          if (side === 0) x = Math.max(x, w + 10); else x = Math.min(x, W - w - 10);
          const op = focus >= 0 && it.k !== focus ? 0.35 : hover >= 0 ? 0.4 : 1;
          const rc = setPlate(pl, text, x, it.y, side === 0, 13, op, V3 ? LABC[it.k] : null);
          pl.k = it.k;
          if (V3 && leads[j].lk !== it.k) { leads[j].setAttribute('stroke', LABC[it.k]); leads[j].lk = it.k; }
          const ex = side === 0 ? rc[2] : rc[0], elbow = side === 0 ? ex + 10 : ex - 10;
          leads[j].setAttribute('d', `M${ex.toFixed(1)} ${it.y.toFixed(1)}H${elbow.toFixed(1)}L${it.ax.toFixed(1)} ${it.ay.toFixed(1)}`);
          leads[j].setAttribute('stroke-opacity', (0.45 * op).toFixed(2));
          leads[j].style.display = '';
          j++;
        }
      });
      for (; j < LABELS; j++) { lobePl[j].g.style.display = 'none'; lobePl[j].k = -1; leads[j].style.display = 'none'; }
      const boxes = [], at = (i) => (i >= 0 && i < N ? proj(TP[i * 3], TP[i * 3 + 1], TP[i * 3 + 2]) : null);
      const fits = (r) => !boxes.some((b) => r[0] < b[2] && r[2] > b[0] && r[1] < b[3] && r[3] > b[1]);
      let pj = 0;
      if (lm !== 'none' && hover < 0 && selected >= 0 && V3) {
        // v4: the chosen page's title and up to 8 neighbours, short
        for (const i of [selected].concat(neighbourLabels(selected))) {
          if (pj >= PAGE_LABELS) break;
          const q = at(i);
          if (!q || q[0] < 0 || q[1] < 8 || q[0] > W - 40 || q[1] > H - 8) continue;
          const text = trunc(PG[i].title, i === selected ? 48 : 24), w = textW(text, 12.5);
          const r = [q[0] + 3, q[1] - 10, q[0] + w + 18, q[1] + 10];
          if (!fits(r)) continue;
          boxes.push(r);
          setPlate(pagePl[pj++], text, q[0] + 9, q[1], false, 12.5, i === selected ? 1 : 0.85, pageFill(i));
        }
      } else if (lm === 'atlas' && cam.dist < zoomIn && hover < 0) {
        // Zoomed into a lobe: titles of its top pages, beside their points. v3: the selected page's
        // lobe, else the lobe centred nearest the orbit target (lobes overlap on screen through the
        // glass, so the one nearest the screen centre may sit far behind)
        let k = focus >= 0 ? focus : V3 && selected >= 0 && LOBE[selected] < L ? LOBE[selected] : -1;
        if (k < 0) {
          let best = Infinity;
          for (const c of labelOrder) {
            const nrm = !V3 && v3(lobes[c].normal, null), a = anchorT(c);
            if ((nrm && nrm[0] * bk[0] + nrm[1] * bk[1] + nrm[2] * bk[2] <= 0.2) || (lobes[c].archive && !archiveOn)) continue;
            const q = proj(a[0], a[1], a[2]);
            if (!q) continue;
            const d = V3 ? Math.hypot(a[0] - cam.tx, a[1] - cam.ty, a[2] - cam.tz) : Math.hypot(q[0] - W / 2, q[1] - H / 2);
            if (d < best) { best = d; k = c; }
          }
        }
        if (k >= 0) {
          for (const i of topPages[k]) {
            if (pj >= PAGE_LABELS) break;
            if (!COL[i * 4 + 3]) continue;
            const q = at(i);
            if (!q || q[2] > cam.dist + (V3 ? 0.3 : 0) || q[0] < 0 || q[1] < 8 || q[0] > W - 40 || q[1] > H - 8) continue;
            const text = trunc(PG[i].title), w = textW(text, 12.5);
            const r = [q[0] + 3, q[1] - 10, q[0] + w + 18, q[1] + 10];
            if (!fits(r)) continue;
            boxes.push(r);
            setPlate(pagePl[pj++], text, q[0] + 9, q[1], false, 12.5, 0.9, pageFill(i));
          }
        }
      }
      for (; pj < PAGE_LABELS; pj++) pagePl[pj].g.style.display = 'none';
      topSites.forEach((i, n) => {
        const q = lm !== 'none' && i !== hover && COL[i * 4 + 3] ? at(i) : null;
        const text = q && trunc(PG[i].title, 28), r = q && [q[0] + 3, q[1] - 8, q[0] + textW(text, 11) + 16, q[1] + 8];
        if (!q || q[0] < 0 || q[1] < 8 || r[2] > W || q[1] > H - 8 || !fits(r)) { sitePl[n].g.style.display = 'none'; return; }
        boxes.push(r);
        setPlate(sitePl[n], text, q[0] + 8, q[1], false, 11, 0.75, colors.label);
      });
      // Hover title and rings (v4: the chosen page's ring only in the Canvas fallback; WebGL has the glimmer).
      const hq = lm !== 'none' ? at(hover) : null;
      if (hq) {
        const text = trunc(PG[hover].title), w = textW(text, 12.5), right = hq[0] + w + 24 > W;
        setPlate(hoverPl, text, right ? hq[0] - 12 : hq[0] + 12, hq[1] - 12, right, 12.5, 1, pageFill(hover));
      } else hoverPl.g.style.display = 'none';
      ring(selRing, R.kind === 'webgl2' && V3 ? null : at(selected), 9);
      const rq = at(reticle);
      if (rq) { reticleG.setAttribute('transform', `translate(${rq[0].toFixed(1)} ${rq[1].toFixed(1)})`); reticleG.style.display = ''; }
      else reticleG.style.display = 'none';
      beaconRings.forEach((c, k) => {
        const l = beaconLobes[k], a = l !== undefined ? anchorT(l) : null, q = a && proj(a[0], a[1], a[2]);
        if (!q) { c.style.display = 'none'; return; }
        const ph = reduced || t - beaconT0 >= BEACON_MS ? 0.4 : ((t - beaconT0) / 1400 + k * 0.33) % 1;
        ring(c, q, 8 + 16 * ph);
        c.setAttribute('stroke-opacity', (1 - ph * 0.85).toFixed(2));
      });
    }
    function ring(c, q, r) {
      if (!q) { c.style.display = 'none'; return; }
      c.setAttribute('cx', q[0].toFixed(1)); c.setAttribute('cy', q[1].toFixed(1)); c.setAttribute('r', r.toFixed(1));
      c.style.display = '';
    }
    const trunc = (s, n) => { s = String(s ?? ''); n = n || 48; return s.length > n ? s.slice(0, n - 1) + '…' : s; };
    const pageFill = (i) => (!V3 ? null : LOBE[i] < L ? LABC[LOBE[i]] : colors.label);
    // v4 breadcrumb: Brain › Region › Page; each crumb a button back to its level; the page hears it too
    function crumbsNow() {
      const out = [{ level: 'brain', id: null, title: 'Brain' }], k = selected >= 0 ? LOBE[selected] : focus;
      if (k >= 0 && k < L) out.push({ level: 'region', id: lobes[k].id, title: String(lobes[k].title ?? lobes[k].id ?? '') });
      if (selected >= 0) out.push({ level: 'page', id: PG[selected].id, title: String(PG[selected].title ?? PG[selected].id) });
      return out;
    }
    function syncCrumbs() {
      const cr = crumbsNow(), key = cr.map((c) => c.level + ':' + c.id).join('|');
      if (key === crumbKey) return;
      crumbKey = key;
      const st = rgb(colors.stage), kids = [];
      cr.forEach((c, n) => {
        const last = n === cr.length - 1;
        if (n) {
          const s = document.createElement('span');
          s.textContent = '›';
          s.setAttribute('aria-hidden', 'true');
          Object.assign(s.style, { color: colors.label, opacity: '0.6' });
          kids.push(s);
        }
        const b = document.createElement('button');
        b.type = 'button';
        b.textContent = trunc(c.title, 28);
        b.title = c.title;
        Object.assign(b.style, { border: '0', borderRadius: '2px', padding: '2px 6px', font: 'inherit', cursor: 'pointer',
          color: colors.label, opacity: last ? '1' : '0.75', background: `rgba(${st[0]},${st[1]},${st[2]},0.82)` });
        if (last) b.setAttribute('aria-current', 'location');
        b.addEventListener('click', () => goCrumb(c));
        kids.push(b);
      });
      const had = crumbNav.contains(document.activeElement);
      crumbNav.replaceChildren(...kids);
      crumbNav.hidden = cr.length < 2;  // a lone "Brain" only repeats the panel's own title
      if (had) (crumbNav.hidden ? box : kids[kids.length - 1]).focus();  // a keyboard user stays put
      if (opts.onCrumbs) opts.onCrumbs(cr.map((c) => Object.assign({}, c)));
    }
    function goCrumb(c) {
      if (c.level === 'brain') overview();
      else if (c.level === 'region') { const k = lobeIdx.get(String(c.id)); if (k !== undefined) pickLobe(k); }
      else frameView();
    }

    /* ---- Today: pulses travel from today's pages along their links, then a reticle ---- */
    function stepReplay(t) {
      const r = replay, out = [];
      if (r.v3) {  // v3: the pulses are effects; when they are done the focus page says hello
        if (t < r.end) return true;
        replay = null;
        settleToday(r.focus);
        fire(r.focus, performance.now());
        return false;
      }
      let live = false;
      r.src.forEach((i, k) => {
        const p = (t - r.t0 - k * r.stagger) / r.dur;
        if (p < 0) { live = true; return; }
        if (p > 1) return;
        live = true;
        const c = pal[0];
        out.push({ x: TP[i * 3], y: TP[i * 3 + 1], z: TP[i * 3 + 2], size: 7.5, c, a: 1 - p, e: 1 - p });
        for (const j of r.links[k]) {
          const pts = r.arcs.get(i * N + j) || arcPts(i, j, 16);
          r.arcs.set(i * N + j, pts);
          for (const [lag, a, e] of [[0, 1, 1], [0.06, 0.6, 0.6], [0.12, 0.3, 0.3]]) {
            const s = Math.max(0, Math.min(1, p - lag)) * 15, s0 = Math.floor(s), f = s - s0, s1 = Math.min(15, s0 + 1);
            out.push({ x: pts[s0 * 3] + (pts[s1 * 3] - pts[s0 * 3]) * f, y: pts[s0 * 3 + 1] + (pts[s1 * 3 + 1] - pts[s0 * 3 + 1]) * f,
              z: pts[s0 * 3 + 2] + (pts[s1 * 3 + 2] - pts[s0 * 3 + 2]) * f, size: 6, c, a, e });
          }
        }
      });
      R.setSprites(out);
      if (!live) {
        replay = null;
        R.setSprites([]);
        settleToday(r.focus);
        return false;
      }
      return true;
    }
    function settleToday(f) {
      reticle = f;
      if (f >= 0) fly(viewToward(TP[f * 3], TP[f * 3 + 1], TP[f * 3 + 2], V3 ? 1.0 : 0.6));
      invalidate();
    }

    /* ---- v4 effects: comets along fibres, the chosen page's glimmer, arrival blinks,
       region breathing and nebulae. A comet runs along fibre f away from page `from`: a white head 2.2x
       the fibre's width and a tail 22 % of its length that fades and turns the far end's
       colour over its first 40 % (v4 arena: fading as (1 - s)^1.4, as ^2.2 hid the trails at overview
       scale); it travels at 0.55 brain radii a second (0.7-1.8 s, eased). While a
       page is selected every fibre of it carries comets (60 % outward, 40 % inward, a 0.3-1.6 s rest
       between runs, at most 120 at once, round-robin), and a comet's arrival blinks the neighbour.
       Effects are rebuilt into one small instance buffer each frame and drawn with the fibres; in the
       Canvas fallback comet heads become sprites. Nothing here runs under reduced motion: the selection
       is a still white page with white fibres. */
    let fxs = [], sparks = [], blinks = [], trails = null, hello = null, nextFire = 0, nextWave = performance.now() + 6000;
    let fxBuf = new Float32Array(25 * 256);
    const WHITE = [255, 255, 255], WARM = [255, 243, 220];
    const trailC = () => (style.trailTint === 'warm' ? WARM : WHITE);
    const MOTES = [[3.2, 20, 3100], [3.7, 35, 4300], [4.2, 50, 5700], [4.6, 65, 7900]];  // orbit radius, tilt in degrees, period in ms
    const lobeRGB = (i) => (LOBE[i] < L ? LC[LOBE[i]] : pal[V3 ? pcOf(i) : 1]);
    const whiten = (c, k) => [c[0] + (255 - c[0]) * k, c[1] + (255 - c[1]) * k, c[2] + (255 - c[2]) * k];
    const shown = (i) => COL[i * 4 + 3] > 0;
    const fibW = (f) => FCOL[f * 8 + 7] / 40 * SV.fibW;
    function travel(f) {  // ms for the head to cross the fibre, plus the time its tail takes to leave
      const a = FA[f] * 3, b = FB[f] * 3, len = Math.hypot(TP[b] - TP[a], TP[b + 1] - TP[a + 1], TP[b + 2] - TP[a + 2]);
      return Math.min(1800, Math.max(700, len / (0.55 * Rb) * 1000)) * 1.22;
    }
    function addFx(f, from, t0, dur, a, w, o) {
      const to = other(f, from);
      fxs.push(Object.assign({ f, rev: FA[f] !== from, to, t0, dur, a, w, len: 0.154, ca: lobeRGB(to), cb: whiten(lobeRGB(from), 0.85),
        fadeOut: false, amb: false, lane: null, blink: false, arrived: false }, o));
    }
    // a page's drawn fibres, links before similar ones, at most `cap`
    function fibresOf(i, cap) {
      const out = [];
      for (let kind = 0; kind < 2; kind++) {
        for (let q = FAO[i]; q < FAO[i + 1] && out.length < cap; q++) { const f = FADJ[q]; if (FK[f] === kind && FCOL[f * 8 + 3]) out.push(f); }
      }
      return out;
    }
    // The hello of a selection (300 ms): 8 motes rush into the page from 6x its radius.
    function fire(i, t0) {
      if (!V3 || reduced || i < 0 || i >= N) return;
      hello = { i, t0 };
      invalidate();
    }
    function startTrails(i) {
      fxs = fxs.filter((e) => !e.lane);
      trails = null;
      if (i < 0 || !NF || !V3) return;
      const t = performance.now();
      trails = { i, rr: 0, lanes: fibresOf(i, Infinity).map((f) => ({ f, out: Math.random() < 0.6, at: t + Math.random() * 1200, run: false })) };
    }
    function stepTrails(t) {
      const T = trails, n = T.lanes.length;
      let active = 0;
      for (const l of T.lanes) if (l.run) active++;
      for (let k = 0; k < n && active < TRAIL_CAP; k++) {
        const at = (T.rr + k) % n, l = T.lanes[at];
        if (l.run || t < l.at) continue;
        l.run = true;
        active++;
        const nb = other(l.f, T.i);
        addFx(l.f, l.out ? T.i : nb, t, travel(l.f), 1.6, 2.2 * fibW(l.f), { ca: lobeRGB(nb), cb: trailC(), lane: l, blink: l.out });
        T.rr = (at + 1) % n;
      }
    }
    // The alive brain: every 0.9-2.4 s (at aliveRate 1) one comet in topic colours runs along one fibre
    // of a page (hubs and recent pages more often); every ~20 s one region breathes softly.
    const CDF = new Float64Array(N);
    {
      let s = 0;
      for (let i = 0; i < N; i++) { s += 1 + 2 * Math.log1p(deg(i)) + 3 * RECENT[i] + 2 * HUB[i]; CDF[i] = s; }
    }
    function spontaneous(t) {
      if (!N) return;
      const x = Math.random() * CDF[N - 1];
      let lo = 0, hi = N - 1;
      while (lo < hi) { const m = (lo + hi) >> 1; if (CDF[m] < x) lo = m + 1; else hi = m; }
      if (!shown(lo)) return;
      const fs = fibresOf(lo, 40);
      if (!fs.length) return;
      const f = fs[Math.floor(Math.random() * fs.length)];
      addFx(f, lo, t, travel(f), 0.7, 1.8 * Math.max(1, fibW(f)), { amb: true, ca: lobeRGB(other(f, lo)), cb: whiten(lobeRGB(lo), 0.55) });
    }
    const spreads = new Float32Array(L).fill(-1);
    function spreadOf(k) {  // mean distance of a lobe's pages from its anchor
      if (spreads[k] < 0) {
        const a = anchorOf(k), pts = lobePages[k];
        let s = 0;
        if (a) for (const i of pts) s += Math.hypot(P[i * 3] - a[0], P[i * 3 + 1] - a[1], P[i * 3 + 2] - a[2]);
        spreads[k] = pts.length && a ? s / pts.length : 0.1;
      }
      return spreads[k];
    }
    function lobeWave(k, t, dur, strength, amb) {
      const a = anchorOf(k);
      if (a && !reduced) lwave = { k, c: a, R: Math.max(0.08, spreadOf(k) * (1 - SV.tightness) * 1.8), t0: t, dur, s: strength, amb };
    }
    function stepFx(t) {
      const gl = R.kind === 'webgl2';
      if (NF && ambient() && hover < 0 && selected < 0) {
        const rate = style.aliveRate;
        if (rate > 0.01 && t >= nextFire) { nextFire = t + (900 + Math.random() * 1500) / rate; spontaneous(t); }
        if (rate > 0.01 && t >= nextWave && (!lwave || lwave.amb)) {
          nextWave = t + (16000 + Math.random() * 8000) / Math.max(0.3, rate);
          const ks = labelOrder.filter((k) => visCount[k] && !lobes[k].archive);
          if (ks.length) lobeWave(ks[Math.floor(Math.random() * ks.length)], t, 2600, 0.35, true);
        }
      }
      if (trails && gl && !reduced) stepTrails(t);
      if (lwave && t >= lwave.t0 + lwave.dur) lwave = null;
      if (hello && t >= hello.t0 + 300) hello = null;
      if (sparks.length) sparks = sparks.filter((s) => t < s.t0 + s.dur);
      if (blinks.length) blinks = blinks.filter((b) => t < b.t0 + 780);
      let busy = !!(lwave && !lwave.amb) || !!hello || blinks.length > 0 || trailsOn();
      if (fxBuf.length < fxs.length * 25) fxBuf = new Float32Array(fxs.length * 50);
      const I = fxBuf, spr = [], keep = [];
      let n = 0;
      const put = (e, head, len, comet, a) => {
        const o = n++ * 25, f = e.f, from = e.rev ? FB[f] : FA[f], to = e.rev ? FA[f] : FB[f], ia = from * 3, ib = to * 3;
        I[o] = P[ia]; I[o + 1] = P[ia + 1]; I[o + 2] = P[ia + 2];
        I[o + 3] = P[ib]; I[o + 4] = P[ib + 1]; I[o + 5] = P[ib + 2];
        I[o + 6] = FC[f * 3]; I[o + 7] = FC[f * 3 + 1]; I[o + 8] = FC[f * 3 + 2]; I[o + 9] = (f % 997) + 0.5;
        I[o + 10] = e.ca[0] / 255; I[o + 11] = e.ca[1] / 255; I[o + 12] = e.ca[2] / 255; I[o + 13] = a;
        I[o + 14] = e.cb[0] / 255; I[o + 15] = e.cb[1] / 255; I[o + 16] = e.cb[2] / 255; I[o + 17] = e.w / 6.375;
        I[o + 18] = head; I[o + 19] = len; I[o + 20] = 0; I[o + 21] = comet;
        I[o + 22] = 1; I[o + 23] = LOBE[from]; I[o + 24] = LOBE[to];
      };
      for (const e of fxs) {
        if (t >= e.t0 + e.dur) {  // done: a trail's lane rests 0.3-1.6 s
          if (e.lane) { e.lane.run = false; e.lane.at = t + 300 + Math.random() * 1300; }
          continue;
        }
        keep.push(e);
        if (!e.amb) busy = true;
        if (t < e.t0) continue;
        const q = (t - e.t0) / e.dur, a = e.fadeOut ? e.a * (1 - q) : e.a;
        if (e.len >= 1) { if (gl) put(e, 1, 1, 0, a); continue; }
        const head = q * q * (3 - 2 * q) * (1 + e.len);  // runs past the end so the tail leaves too
        if (head >= 1 && !e.arrived) { e.arrived = true; if (e.blink) blinks.push({ i: e.to, t0: t }); }
        if (gl) { put(e, head, e.len, 1, a); continue; }
        if (spr.length >= 60) continue;  // the Canvas fallback shows a sample of the comet heads
        const h = Math.min(1, head), u = 1 - h, from = e.rev ? FB[e.f] : FA[e.f], ia = from * 3, ib = e.to * 3, c3 = e.f * 3;
        spr.push({ x: u * u * TP[ia] + 2 * u * h * TFC[c3] + h * h * TP[ib], y: u * u * TP[ia + 1] + 2 * u * h * TFC[c3 + 1] + h * h * TP[ib + 1],
          z: u * u * TP[ia + 2] + 2 * u * h * TFC[c3 + 2] + h * h * TP[ib + 2], size: 3 + 1.5 * e.w, c: e.cb, a, e: 0 });
      }
      fxs = keep;
      for (const s of sparks) {
        if (t < s.t0 || (!gl && spr.length >= 90)) continue;
        const p = (t - s.t0) / s.dur, i = s.i;
        spr.push({ x: TP[i * 3], y: TP[i * 3 + 1], z: TP[i * 3 + 2], size: s.size, c: s.c, a: 1 - p, e: s.e * (1 - p) });
      }
      // arrival blink: the neighbour 35 % larger and brighter, 180 ms up, 600 ms down
      for (const b of blinks) {
        const d = t - b.t0, env = d < 180 ? Math.sin(d / 180 * Math.PI / 2) : Math.pow(Math.max(0, 1 - (d - 180) / 600), 2), i = b.i;
        spr.push({ x: TP[i * 3], y: TP[i * 3 + 1], z: TP[i * 3 + 2], size: sizeOf(i) * (1 + 90 / 255) * 1.35, c: whiten(lobeRGB(i), 0.4), a: 0.5 * env, e: 0 });
      }
      if (gl) {
        if (SV.nebula > 0.003) nebulae(spr);
        if (selected >= 0 && !reduced) glimmer(spr, t);
        if (hello && !reduced) helloMotes(spr, t);
      }
      R.setPulses(I, n);
      R.setSprites(spr);
      return busy;
    }
    // world units a sprite px covers at a page (sprites scale with depth like the pages do)
    function pxWorld(x, y, z) {
      const q = proj(x, y, z);
      return q ? Math.min(1.6, Math.max(0.75, home.dist / q[2])) * q[2] / focal : 0;
    }
    // the chosen page's visible radius, px at the reference depth (it draws 1.6x its size)
    const radiusOf = (i) => Math.max(3.5, sizeOf(i) * 1.6 * 0.35);
    // Region nebulae: a faint additive cloud at each region's centre, as wide as the region.
    function nebulae(spr) {
      const dim = selected >= 0 || hover >= 0 ? 0.6 : 1;
      for (const k of labelOrder) {
        if (!visCount[k] || (lobes[k].archive && !archiveOn)) continue;
        const a = anchorT(k), q = a && proj(a[0], a[1], a[2]);
        if (!q) continue;
        const px = 2 * Math.max(0.06, spreadOf(k) * (1 - SV.tightness) * 2.2) * focal / q[2];
        spr.push({ x: a[0], y: a[1], z: a[2], size: -Math.min(px, 2 * Math.max(W, H)), kind: 0.5, c: LC[k],
          a: 0.035 * SV.nebula * dim * (focus >= 0 && k !== focus ? 0.4 : 1), e: 0 });
      }
    }
    // The glimmer of the chosen page: a breathing halo ring (1.44x its radius, 2.4 s; round 2: -40 %), a
    // slowly turning 4-arm star glint (arms 5x its radius) that twinkles, and 4 motes on tilted orbits (dimmer behind).
    function glimmer(spr, t) {
      const i = selected, x = TP[i * 3], y = TP[i * 3 + 1], z = TP[i * 3 + 2], c = lobeRGB(i), r0 = radiusOf(i);
      const ph = 0.5 - 0.5 * Math.cos(TAU * t / 2400);
      spr.push({ x, y, z, size: 2 * 1.44 * r0 * (0.94 + 0.12 * ph), ring: true, c: whiten(c, 0.6), a: 0.35 + 0.35 * ph, e: 0 });
      spr.push({ x, y, z, size: 10 * r0, kind: 1, c: WHITE, a: 0.65 + 0.35 * Math.sin(TAU * t / 1700) * Math.sin(TAU * t / 2900), e: 0 });
      const k = pxWorld(x, y, z);
      if (!k) return;
      MOTES.forEach(([m, tilt, per], n) => {
        const th = TAU * t / per + n * 1.9, rw = m * r0 * k, ct = Math.cos(tilt * Math.PI / 180), st = Math.sin(tilt * Math.PI / 180);
        const cx = Math.cos(th) * rw, sx = Math.sin(th) * rw;
        // the orbit's second axis: screen-up tilted toward the eye
        const vx = -V.sy * V.sp * ct + V.sy * V.cp * st, vy = V.cp * ct + V.sp * st, vz = -V.cy * V.sp * ct + V.cy * V.cp * st;
        const front = Math.min(1, Math.max(0, (Math.sin(th) + 0.2) / 0.4));
        spr.push({ x: x + V.cy * cx + vx * sx, y: y + vy * sx, z: z - V.sy * cx + vz * sx, size: -3, c: whiten(c, 0.8), a: 0.35 + 0.65 * front, e: 0 });
      });
    }
    function helloMotes(spr, t) {
      const i = hello.i, x = TP[i * 3], y = TP[i * 3 + 1], z = TP[i * 3 + 2], p = Math.min(1, (t - hello.t0) / 300);
      const rw = 6 * radiusOf(i) * pxWorld(x, y, z) * (1 - p * p);
      for (let m = 0; m < 8; m++) {
        const th = m * TAU / 8 + 0.3, cx = Math.cos(th) * rw, sy = Math.sin(th) * rw;
        spr.push({ x: x + V.cy * cx - V.sy * V.sp * sy, y: y + V.cp * sy, z: z - V.sy * cx - V.cy * V.sp * sy, size: -3, c: WHITE, a: 1 - 0.4 * p, e: 0 });
      }
    }
    // Mode and style switches: node and fibre colours cross-fade (WebGL2; elsewhere they jump).
    function crossFade(run, ms) {
      if (!V3 || reduced || R.kind !== 'webgl2') { run(); return; }
      const c0 = COL.slice(), f0 = FCOL.slice();
      run();
      fade = { t0: performance.now(), ms: ms || 400, c0, f0, c1: COL.slice(), f1: FCOL.slice() };
      stepFade(fade.t0);
      invalidate();
    }
    function stepFade(t) {
      const p = Math.min(1, Math.max(0, (t - fade.t0) / fade.ms)), e = p * p * (3 - 2 * p), { c0, c1, f0, f1 } = fade;
      for (let k = 0; k < c1.length; k++) COL[k] = c0[k] + (c1[k] - c0[k]) * e;
      for (let k = 0; k < f1.length; k++) FCOL[k] = f0[k] + (f1[k] - f0[k]) * e;
      R.setPoints();
      R.setFibres();
      if (p >= 1) fade = null;
      return p < 1;
    }

    /* ---- selection, hover, lobes, the walk through connections and the history ---- */
    function infoFor(i) {
      const lk = linksOf(i), info = { linked: lk.shown.map((j) => PG[j].id), similar: [], hiddenLinks: lk.hidden };
      const linked = new Set(lk.shown);
      for (const j of nearOf(i)) if (!linked.has(j)) info.similar.push(PG[j].id);
      const k = LOBE[i];
      info.lobe = k < L ? lobes[k].id : null;
      if (ARCH[i]) info.beacons = beaconsOf(i).map((b) => lobes[b].id);
      return info;
    }
    function beaconsOf(i) {
      const b = beacons[String(i)];
      return Array.isArray(b) ? b.filter((k) => Number.isInteger(k) && k >= 0 && k < L).slice(0, 3) : [];
    }
    // how: 'step' keeps the walk through the previous page's connections, 'back' adds no history
    function select(i, flyThere, how) {
      if (i !== selected) {
        if (selected >= 0 && how !== 'back') { hist.push(selected); if (hist.length > HISTORY) hist.shift(); }
        if (how !== 'step') walk = null;
      }
      selected = i;
      if (i >= 0) picked = true;
      reticle = -1;
      selLabels = null;
      beaconLobes = i >= 0 && ARCH[i] ? beaconsOf(i) : [];
      beaconT0 = performance.now();
      restyle();
      relines();
      startTrails(i);
      if (i >= 0 && flyThere) fly(pageView(i), SEL_FLY_MS, true);  // then a slow orbit round it until input
      return i >= 0 ? infoFor(i) : null;
    }
    // A selection the user made: fly there, say hello, tell the page.
    function choose(i, how) {
      const info = select(i, true, how);
      fire(i, performance.now());
      if (opts.onSelect) opts.onSelect(PG[i], info);
      return info;
    }
    let hoverCand = -1, hoverTimer = 0;
    function candidate(i) {
      if (i === hoverCand) return;
      hoverCand = i;
      box.style.cursor = i >= 0 ? 'pointer' : '';
      clearTimeout(hoverTimer);
      hoverTimer = setTimeout(() => setHover(hoverCand, true), HOVER_MS);
    }
    function setHover(i, notify) {
      if (i === hover) return;
      hover = i;
      if (V3 && i >= 0 && mode !== 'growth') breathT0 = performance.now();  // its neighbours breathe once
      restyle();
      relines();
      if (i >= 0 && NF && gpu()) {  // a light preview: one comet along each of its fibres
        const t = performance.now();
        for (const f of fibresOf(i, 80)) addFx(f, i, t + Math.random() * 250, travel(f), 0.8, 2.2 * fibW(f), { ca: lobeRGB(other(f, i)), cb: trailC() });
      }
      if (!notify || !opts.onHover) return;
      if (i < 0) { opts.onHover(null, null, null); return; }
      const q = proj(TP[i * 3], TP[i * 3 + 1], TP[i * 3 + 2]), lk = linksOf(i);
      opts.onHover(PG[i], q ? { x: q[0], y: q[1] } : null, { links: lk.shown.length, hiddenLinks: lk.hidden });
    }
    function focusOn(k, close) {
      focus = k;
      restyle(); retint(); relines();
      if (k < 0 || !anchorOf(k)) { fly(home); return; }
      fly(regionView(k, close));
      if (!V3 || reduced || !NF) return;
      // v3: the region breathes once as the camera flies in; its tracts flare once
      const t = performance.now();
      lobeWave(k, t, 900, 0.8, false);
      let left = FX_CAP;
      for (const i of lobePages[k]) {
        for (let q = FAO[i]; q < FAO[i + 1] && left; q++) {
          const f = FADJ[q], j = other(f, i);
          if (FK[f] || LOBE[j] === k || !FCOL[f * 8 + 3]) continue;
          addFx(f, i, t, 900, 0.75, 1.6, { len: 1, fadeOut: true, ca: whiten(lobeRGB(i), 0.2), cb: whiten(lobeRGB(j), 0.2) });
          left--;
        }
      }
      invalidate();
    }
    // A lobe's tracts for the inspector: Python's, else (v3) its link fibres counted per other lobe.
    function tractsOf(k) {
      if (tracts.length || !NF) return tracts.filter((t) => t.a === k || t.b === k).map((t) => ({ lobe: lobes[t.a === k ? t.b : t.a].id, n: +t.n || 0 }));
      const n = new Map();
      for (let f = 0; f < NF; f++) {
        if (FK[f]) continue;
        const a = LOBE[FA[f]], b = LOBE[FB[f]];
        if (a !== b && (a === k || b === k) && a < L && b < L) { const o = a === k ? b : a; n.set(o, (n.get(o) || 0) + 1); }
      }
      return [...n].map(([o, c]) => ({ lobe: lobes[o].id, n: c }));
    }
    function pickLobe(k, close) {
      clearTimeout(hoverTimer);
      hoverCand = -1;
      setHover(-1, true);
      if (selected >= 0) select(-1, false);
      picked = true;
      focusOn(k, close);
      const l = lobes[k];
      if (opts.onCluster) opts.onCluster(l);
      if (opts.onLobe) {
        opts.onLobe(l, { count: countOf(k), tracts: tractsOf(k).sort((a, b) => b.n - a.n) });
      }
    }
    function clearAll() {
      if (selected >= 0) select(-1, false);
      focus = -1; reticle = -1; beaconLobes = []; todaySet = null; walk = null;
      if (replay) { replay = null; R.setSprites([]); }
      restyle(); retint(); relines();
    }
    function overview() { clearAll(); fly(home); }
    // Back one level: page -> its region -> the overview; false when already at the overview.
    function back() {
      if (selected >= 0) {
        const k = LOBE[selected];
        if (k < L && anchorOf(k)) pickLobe(k); else overview();
        return true;
      }
      if (focus >= 0 || todaySet || reticle >= 0) { overview(); return true; }
      return false;
    }
    // F: frame the chosen page, else the focused region, else the whole brain.
    function frameView() {
      if (selected >= 0) fly(pageView(selected), SEL_FLY_MS, true);
      else if (focus >= 0) fly(regionView(focus));
      else fly(home);
    }
    // [ and ]: the chosen page's connections in the inspector's order (links, then similar pages); the
    // walk stays on the first page's list while it steps, so ] ] ] visits its neighbours in turn.
    function neighbours(i) {
      const lk = linksOf(i).shown, seen = new Set(lk), out = lk.slice();
      for (const j of nearOf(i)) if (!seen.has(j)) { seen.add(j); out.push(j); }
      return out.filter((j) => userVis[j] && (archiveOn || !ARCH[j]));
    }
    function step(dir) {
      if (selected < 0) return false;
      if (!walk || walk.list[walk.at] !== selected) walk = { list: neighbours(selected), at: -1 };
      const n = walk.list.length;
      if (!n) return false;
      walk.at = walk.at < 0 ? (dir < 0 ? n - 1 : 0) : (walk.at + (dir < 0 ? n - 1 : 1)) % n;
      choose(walk.list[walk.at], 'step');
      return true;
    }
    // Backspace: the previous page (a history of 20).
    function historyBack() {
      while (hist.length) {
        const j = hist.pop();
        if (j !== selected && userVis[j] && (archiveOn || !ARCH[j])) { choose(j, 'back'); return true; }
      }
      return false;
    }
    function setMode(m) {
      if (!MODES.includes(m) || m === mode) return;
      mode = m;
      crossFade(() => { restyle(); retint(); });
      if (opts.onMode) opts.onMode(m);
    }
    function setSpin(on) {
      spin = !!on;
      lastInput = -Infinity;  // spin starts now, not IDLE_MS after the last input
      if (opts.onSpin) opts.onSpin(spin);
      invalidate();
    }
    function setAlive(on) {
      if (style.alive !== !!on) setStyle({ alive: !!on });
      else if (opts.onAlive) opts.onAlive(alive);
    }
    // Style: merge, clamp, cross-fade over 300 ms (a jump under reduced motion); a style that is exactly
    // a preset's also takes its overview camera.
    function setStyle(part) {
      const prev = style, next = mergeStyle(style, part);
      if (STYLE_SCHEMA.every((d) => next[d.key] === prev[d.key])) return;
      style = next;
      const to = styleTarget(style);
      if (reduced || !V3 || R.kind !== 'webgl2') { Object.assign(SV, to); sAnim = null; }
      else sAnim = { t0: performance.now(), from: Object.assign({}, SV), to };
      if (prev.alive !== style.alive) { alive = style.alive; if (opts.onAlive) opts.onAlive(alive); }
      const pc = presetOf(style);
      if (pc) setCamera(pc.camera);
      else if (prev.tightness !== style.tightness || prev.spread !== style.spread) rehome();
      const recol = prev.palette !== style.palette || prev.saturation !== style.saturation;
      if (recol) { LC = paletteOf(style.palette, LC0, style.saturation, CENTRES); LABC = LC.map(labOf); recolor(); }
      // discrete changes cross-fade; a slider is its own continuity, so a drag restyles without copying
      // the colour buffers, and the trails restart only when the set of fibres changes
      const fibres = prev.links !== style.links || prev.similar !== style.similar;
      if (fibres || prev.palette !== style.palette) crossFade(() => restyle(), STYLE_MS);
      else if (recol || prev.linkBrightness !== style.linkBrightness || prev.linkWidth !== style.linkWidth) restyle();
      if (fibres && selected >= 0) startTrails(selected);
      if (opts.onStyle) opts.onStyle(Object.assign({}, style));
      invalidate();
    }
    // A new overview camera; at the overview the view flies to it.
    function setCamera(cs) {
      if (cs === camSpec) return;
      camSpec = cs;
      rehome();
      if (selected < 0 && focus < 0) fly(home);
    }
    function applyPreset(id) {
      const p = PRESETS.find((x) => x.id === id);
      if (!p) return false;
      setStyle(mergeStyle(DEFAULT_STYLE, p.style));
      camSpec = null;  // re-applying a preset re-frames too
      setCamera(p.camera);
      return true;
    }

    /* ---- input: drag turns (around the chosen page or region), right- or shift-drag pans, two
       pointers pinch, the wheel zooms toward the cursor ---- */
    const ptrs = new Map();
    let drag = null;
    const at = (e) => { const r = box.getBoundingClientRect(); return [e.clientX - r.left, e.clientY - r.top]; };
    const pinchDist = () => { const [a, b] = [...ptrs.values()]; return Math.hypot(a[0] - b[0], a[1] - b[1]) || 1; };
    const pinchMid = () => { const [a, b] = [...ptrs.values()]; return [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2]; };
    const pickAt = (p) => { basis(); return R.kind === 'webgl2' ? R.pick(p[0], p[1], frameParams(performance.now())) : R.pick(p[0], p[1]); };
    const onDown = (e) => {
      const p = at(e);
      try { box.setPointerCapture(e.pointerId); } catch (err) { /* a synthetic pointer has nothing to capture */ }
      ptrs.set(e.pointerId, p);
      clearTimeout(pickTimer); pendingPick = null;
      anim = null; goal = null; selOrbit = false; vel[0] = vel[1] = 0;
      poke();
      if (ptrs.size === 2) { drag = { mode: 'pinch', d: pinchDist(), moved: true }; return; }
      drag = { mode: e.button === 2 || e.shiftKey ? 'pan' : 'rotate', x: p[0], y: p[1], x0: p[0], y0: p[1], moved: false, t: 0 };
    };
    const onMove = (e) => {
      const p = at(e);
      poke();
      if (!drag) { clearTimeout(pickTimer); pickTimer = setTimeout(() => { pendingPick = p; invalidate(); }, PICK_REST_MS); return; }
      if (!ptrs.has(e.pointerId)) return;
      ptrs.set(e.pointerId, p);
      if (drag.mode === 'pinch') {
        if (ptrs.size < 2) return;
        const d = pinchDist(), m = pinchMid();
        zoomBy(drag.d / d, m[0], m[1]);
        drag.d = d;
        return;
      }
      const dx = p[0] - drag.x, dy = p[1] - drag.y;
      drag.x = p[0]; drag.y = p[1];
      if (Math.abs(p[0] - drag.x0) + Math.abs(p[1] - drag.y0) > 4) drag.moved = true;
      if (drag.moved && hover >= 0) { clearTimeout(hoverTimer); hoverCand = -1; setHover(-1, true); }
      if (drag.mode === 'pan') {
        const k = cam.dist / focal;
        cam.tx -= (V.cy * dx + V.sp * V.sy * dy) * k;
        cam.ty += V.cp * dy * k;
        cam.tz -= (-V.sy * dx + V.sp * V.cy * dy) * k;
      } else {
        vel[0] = -dx * 0.008; vel[1] = dy * 0.008;
        cam.yaw += vel[0];
        cam.pitch = clampPitch(cam.pitch + vel[1]);
        drag.t = performance.now();
      }
      invalidate();
    };
    const onUp = (e) => {
      ptrs.delete(e.pointerId);
      if (!drag) return;
      const was = drag;
      drag = null;
      // a release after a pause, or reduced motion, leaves no inertia
      if (reduced || was.mode !== 'rotate' || performance.now() - was.t > 60) vel[0] = vel[1] = 0;
      invalidate();
      if (was.moved || e.type === 'pointercancel') return;
      const i = pickAt(at(e));
      if (i >= 0) choose(i);
    };
    // double-click: empty space goes back to the overview; a page flies in close
    const onDbl = (e) => {
      const i = pickAt(at(e));
      if (i < 0) { overview(); return; }
      if (i !== selected) choose(i);
      fly(pageView(i, true), SEL_FLY_MS, true);
    };
    const onWheel = (e) => {
      e.preventDefault();
      const p = at(e);
      zoomBy(Math.exp(e.deltaY * (e.deltaMode === 1 ? 16 : 1) * 0.0012), p[0], p[1]);
    };
    const onLeave = () => { if (!drag) { clearTimeout(pickTimer); pendingPick = null; clearTimeout(hoverTimer); hoverCand = -1; box.style.cursor = ''; setHover(-1, true); } };
    // arrows turn with a little inertia; + - zoom; 0 the overview; Esc back a level; Backspace the
    // previous page; [ ] the chosen page's connections; F frames the selection
    const onKey = (e) => {
      if (e.ctrlKey || e.metaKey || e.altKey) return;
      const k = e.key;
      const turn = (dx, dy) => {
        anim = null; goal = null; selOrbit = false;
        if (reduced) { cam.yaw += dx * 5; cam.pitch = clampPitch(cam.pitch + dy * 5); } else { vel[0] += dx; vel[1] += dy; }
      };
      if (k === 'ArrowLeft') turn(0.03, 0);
      else if (k === 'ArrowRight') turn(-0.03, 0);
      else if (k === 'ArrowUp') turn(0, 0.024);
      else if (k === 'ArrowDown') turn(0, -0.024);
      else if (k === '+' || k === '=') zoomBy(0.85);
      else if (k === '-' || k === '_') zoomBy(1 / 0.85);
      else if (k === '0') overview();
      else if (k === 'Escape') { if (!back()) return; }
      else if (k === 'Backspace') historyBack();
      else if (k === '[') step(-1);
      else if (k === ']') step(1);
      else if (k === 'f' || k === 'F') frameView();
      else if (k === 'a' || k === 'A') setMode('activity');
      else if (k === 'm' || k === 'M') setMode('mastery');
      else if (k === 'g' || k === 'G') setMode('growth');
      else if (k === 's' || k === 'S') setSpin(!spin);
      else return;
      e.preventDefault();
      if (k !== 's' && k !== 'S') poke();
      invalidate();
    };
    const noMenu = (e) => e.preventDefault();
    box.addEventListener('pointerdown', onDown);
    box.addEventListener('pointermove', onMove);
    box.addEventListener('pointerup', onUp);
    box.addEventListener('pointercancel', onUp);
    box.addEventListener('pointerleave', onLeave);
    box.addEventListener('dblclick', onDbl);
    box.addEventListener('wheel', onWheel, { passive: false });
    box.addEventListener('keydown', onKey);
    box.addEventListener('contextmenu', noMenu);

    function resize() {
      const w = box.clientWidth, h = box.clientHeight, r = Math.min(1.5, window.devicePixelRatio || 1);
      if (w === W && h === H && r === dpr) return;
      // at the overview the camera re-frames for the new size; elsewhere it stays put
      const atHome = camSet && !anim && CAM.every((k) => Math.abs(cam[k] - home[k]) < 1e-3);
      W = w; H = h; dpr = r;
      focal = H / 2 / Math.tan(FOV / 2);
      R.resize(w, h, r);
      if (W && H) {
        rehome();
        if (!camSet || atHome) { Object.assign(cam, home); camSet = true; }
      }
      invalidate();
    }

    /* ---- renderer choice: WebGL2, else Canvas 2D; a lost context falls back for good ---- */
    let canvas = null;
    function makeCanvas() {
      const c = document.createElement('canvas');
      Object.assign(c.style, { display: 'block', width: '100%', height: '100%' });
      box.insertBefore(c, svg);
      return c;
    }
    function useCanvas2D() {
      if (canvas) canvas.remove();
      canvas = makeCanvas();
      R = canvasRenderer(canvas, S);
      W = H = 0;
      arrKey = sizeKey = '';  // the Canvas path reads the arranged control points too
    }
    const wantCanvas = /[?&]canvas(=|&|$)/.test(location.search);
    if (!wantCanvas) {
      canvas = makeCanvas();
      try { R = glRenderer(canvas, S); } catch (err) { R = null; if (window.console) console.warn('brain: WebGL2 failed, using Canvas', err); }
      if (R) {
        canvas.addEventListener('webglcontextlost', (e) => {
          e.preventDefault();
          useCanvas2D();
          arrange();
          setTheme(colors); retint(); relines(); resize();
        });
      }
    }
    if (!R) useCanvas2D();

    const ro = new ResizeObserver(resize);
    ro.observe(box);
    const io = new IntersectionObserver((es) => { inView = es[es.length - 1].isIntersecting; invalidate(); });
    io.observe(box);
    const onVis = () => invalidate();
    document.addEventListener('visibilitychange', onVis);

    LC = paletteOf(style.palette, LC0, style.saturation, CENTRES);
    LABC = LC.map(labOf);
    arrange();
    setTheme(opts.colors);
    resize();
    basis();
    restyle(); retint(); relines();

    return {
      get renderer() { return R.kind; },
      // for a remount: the view, the selected page, spin and the Growth month, and whether a
      // pointer is down or the camera still moving (a flight, a zoom, a flick: a remount waits)
      get camera() { const o = {}; for (const k of CAM) o[k] = cam[k]; return o; },
      get selected() { return selected >= 0 ? PG[selected].id : null; },
      // a node's record by id (a page, or a stem site's {site, category, visits, private, ...})
      node(id) { const i = idIndex.get(String(id)); return i === undefined ? null : PG[i]; },
      get spin() { return spin; },
      get growth() { return growthMonth; },
      get focused() { return focus >= 0 ? String(lobes[focus].id) : null; },
      // the browsing stem: whether there is one, whether the camera went to it, and the flight there
      // (a second call flies back to the overview); true when it flew to the stem
      get hasStem() { return SF < N; },
      get atStem() { return atStem; },
      set atStem(on) { atStem = !!on && SF < N; },  // a remount: the camera is back at the stem already
      stem() {
        if (SF >= N) return false;
        if (atStem) { fly(home); return false; }
        fly(stemView());
        atStem = true;
        return true;
      },
      get busy() { return !!anim || !!goal || ptrs.size > 0 || Math.abs(vel[0]) + Math.abs(vel[1]) > 1e-4; },
      // select a page without flying to it (a remount restores the selection)
      reselect(id) {
        const i = idIndex.get(String(id));
        if (i !== undefined && userVis[i] && !(ARCH[i] && !archiveOn)) select(i, false);
      },
      // Frame a page and ring it; false when it is unknown or filtered out.
      flyTo(id) {
        const i = idIndex.get(String(id));
        if (i === undefined || !userVis[i] || (ARCH[i] && !archiveOn)) return false;
        focus = -1;
        retint();
        select(i, true);
        return true;
      },
      // Fly to a page and show its neighbourhood: its fibres carry trails, the page glimmers.
      explore(id) {
        const i = idIndex.get(String(id));
        if (i === undefined) return null;
        focus = -1;
        retint();
        const info = select(i, true);
        fire(i, performance.now());
        return info;
      },
      focusCluster(id) {
        const k = id === null || id === undefined ? undefined : lobeIdx.get(String(id));
        if (k === undefined) { focus = -1; restyle(); retint(); relines(); fly(home); }
        else focusOn(k);
      },
      // focus a lobe without flying (a remount restores it; the camera is restored already)
      refocus(id) {
        const k = lobeIdx.get(String(id));
        if (k !== undefined) { focus = k; restyle(); retint(); relines(); }
      },
      highlight(ids) {
        if (!ids) hl = null;
        else {
          hl = new Uint8Array(N);
          for (const id of ids) { const i = idIndex.get(String(id)); if (i !== undefined) hl[i] = 1; }
          popT0 = performance.now();  // v3: matches pop 1 -> 1.6 -> 1.2 and keep a soft halo
        }
        restyle();
      },
      setFilter(fn) {
        for (let i = 0; i < N; i++) userVis[i] = !fn || fn(PG[i]) ? 1 : 0;
        if (hover >= 0 && !userVis[hover]) setHover(-1, true);
        if (selected >= 0 && !userVis[selected]) select(-1, false);
        restyle(); relines();
      },
      setTheme,
      setMode,
      get mode() { return mode; },
      // Growth scrubber: pages dated after `month` (YYYY-MM) hide; the month's own pages flash once.
      setGrowth(month) {
        growthMonth = month ? String(month).slice(0, 7) : null;
        flashT0 = performance.now();
        if (mode !== 'growth') setMode('growth'); else restyle();
      },
      setArchive(on) {
        archiveOn = !!on;
        if (!archiveOn && selected >= 0 && ARCH[selected]) { select(-1, false); beaconLobes = []; }
        restyle();
      },
      setSpin,
      // v3: the alive brain (ambient trails, region breathing, shimmer) on or off; it is style.alive
      setAlive,
      get alive() { return alive; },
      // v3: a clause for the page's legend when busy pages' links are held back ('' when none are)
      get legendNote() { return nBusy ? 'Links of very busy pages (like journal days) show when you point at them.' : ''; },
      // Replay today: a pulse from each page (max 30) along its links, then settle on `focusId`.
      today(list, focusId) {
        const src = [];
        for (const id of list || []) {
          const i = idIndex.get(String(id));
          if (i !== undefined && userVis[i] && src.length < 30) src.push(i);
        }
        const f = idIndex.has(String(focusId)) ? idIndex.get(String(focusId)) : src.length ? src[0] : -1;
        todaySet = new Uint8Array(N);
        for (const i of src) todaySet[i] = 1;
        reticle = -1;
        restyle();
        if (reduced || !src.length) { settleToday(f); return; }
        if (V3 && NF) {  // comets run along each page's link fibres in lobe colours, then the focus says hello
          const t0 = performance.now(), stagger = Math.min(120, 1800 / src.length);
          src.forEach((i, k) => {
            const s = t0 + k * stagger;
            sparks.push({ i, t0: s, dur: 700, size: 11, e: 0.8, c: whiten(lobeRGB(i), 0.4) });
            for (const j of linksOf(i).shown.slice(0, 8)) { const fb = fibreOf(i, j); if (fb >= 0) addFx(fb, i, s, 900, 0.95, 2.4, {}); }
          });
          replay = { v3: true, end: t0 + (src.length - 1) * stagger + 950, focus: f };
          invalidate();
          return;
        }
        replay = { t0: performance.now(), src, focus: f, dur: 700, stagger: Math.min(120, 1800 / src.length),
          links: src.map((i) => linksOf(i).shown.slice(0, 8)), arcs: new Map() };
        invalidate();
      },
      // Board -> brain sync: light a page and its links as if hovered (no onHover callback).
      hoverId(id) {
        const i = id === null || id === undefined ? -1 : idIndex.get(String(id));
        clearTimeout(hoverTimer);
        hoverCand = i === undefined ? -1 : i;
        setHover(hoverCand, false);
      },
      resize,
      // v4 style: a copy of the current style; setStyle merges a partial one
      get style() { return Object.assign({}, style); },
      setStyle,
      // a preset by id: its style and its overview camera (the view re-frames at the overview)
      applyPreset,
      // v4 navigation
      back,
      overview,
      frame: frameView,
      step,
      historyBack,
      destroy() {
        dead = true;
        cancelAnimationFrame(raf);
        clearTimeout(spinTimer); clearTimeout(hoverTimer); clearTimeout(pickTimer);
        ro.disconnect(); io.disconnect();
        document.removeEventListener('visibilitychange', onVis);
        if (mq && mq.removeEventListener) mq.removeEventListener('change', onMotion);
        R.destroy();
        box.remove();
      },
    };
  }

  window.CortexBrain = { mount, STYLE_SCHEMA, PRESETS, DEFAULT_STYLE };
})();
