// Models, merged into one vertex-colored geometry each, so the whole city draws in few calls.
// The city is built like Chamberí, in Madrid: closed blocks around courtyards; houses of five to seven floors
// in brick or in ochre, cream and salmon stucco over granite and shop windows; a stone surround, shutters and
// an iron balcony to every window; white glazed miradores; slate mansards with dormers, attics and tiles;
// a tower under a slate cupola where two streets meet.
THREE.ColorManagement.legacyMode = false; // colors below are written in sRGB; before any is made

window.MODELS = (() => {
  const CELL = 1.7; // one window's size, in world units: a floor
  const col = new THREE.Color();
  const tmp = new THREE.Matrix4();
  const shade = (hex, f) => "#" + new THREE.Color(hex).multiplyScalar(f).getHexString();

  class Builder {
    constructor() { this.p = []; this.n = []; this.c = []; this.u = []; }
    // frame: when set, everything added is placed by it too (a model turned or moved as a whole).
    add(geo, matrix, color, win) {
      const g = (geo.index ? geo.toNonIndexed() : geo.clone()).applyMatrix4(this.frame ? this.frame.clone().multiply(matrix) : matrix);
      const pos = g.attributes.position, nor = g.attributes.normal, uv = g.attributes.uv;
      col.set(color);
      for (let i = 0; i < pos.count; i++) {
        this.p.push(pos.getX(i), pos.getY(i), pos.getZ(i));
        this.n.push(nor.getX(i), nor.getY(i), nor.getZ(i));
        this.c.push(col.r, col.g, col.b);
        if (win) { const [a, b] = win(i, uv.getX(i), uv.getY(i), nor.getY(i)); this.u.push(a, b); } else this.u.push(0.002, 0.002);
      }
      return this;
    }
    box(w, h, d, x, y, z, color, windows, rotY = 0) {
      const m = new THREE.Matrix4().makeRotationY(rotY).setPosition(x, y + h / 2, z);
      let win = null;
      if (windows) { // true, or which of the atlas's bays to start from (ox, oy) and how many across (nu)
        const ox = windows.ox ?? Math.floor(Math.random() * 8), oy = windows.oy ?? Math.floor(Math.random() * 8);
        win = (i, u, v) => {
          const face = Math.floor(i / 6); // +x, -x, +y, -y, +z, -z
          if (face === 2 || face === 3) return [0.002, 0.002];
          const across = face < 2 ? d : w;
          const nu = windows.nu || Math.max(1, Math.round(across / CELL)), nv = Math.max(1, Math.round(h / CELL));
          return [(u * nu + ox) / 8, (v * nv + oy) / 8];
        };
      }
      return this.add(new THREE.BoxGeometry(w, h, d), m, color, win);
    }
    // windows: a bay to each side, for a tower. turn: about its own axis.
    cyl(rt, rb, h, x, y, z, color, seg = 18, windows = false, turn = 0) {
      let win = null;
      if (windows) {
        const oy = Math.floor(Math.random() * 8), nv = Math.max(1, Math.round(h / CELL));
        win = (i, u, v, ny) => (Math.abs(ny) > 0.9 ? [0.002, 0.002] : [(u * seg) / 8, (v * nv + oy) / 8]);
      }
      return this.add(new THREE.CylinderGeometry(rt, rb, h, seg), new THREE.Matrix4().makeRotationY(turn).setPosition(x, y + h / 2, z), color, win);
    }
    dome(r, x, y, z, color, squash = 1) {
      const m = new THREE.Matrix4().makeScale(1, squash, 1).setPosition(x, y, z);
      return this.add(new THREE.SphereGeometry(r, 24, 12, 0, Math.PI * 2, 0, Math.PI / 2), m, color);
    }
    frustum(w, d, h, top, x, y, z, color) { // a mansard: four steep sides, a flat top that much smaller
      const m = new THREE.Matrix4().makeTranslation(x, y + h / 2, z).multiply(new THREE.Matrix4().makeScale(w, 1, d)).multiply(new THREE.Matrix4().makeRotationY(Math.PI / 4));
      return this.add(new THREE.CylinderGeometry(top * Math.SQRT1_2, Math.SQRT1_2, h, 4, 1), m, color);
    }
    ball(r, x, y, z, color) {
      return this.add(new THREE.SphereGeometry(r, 10, 7), tmp.clone().makeTranslation(x, y, z), color);
    }
    barrel(r, len, x, y, z, color, alongX = true) { // half a cylinder lying down
      const g = new THREE.CylinderGeometry(r, r, len, 16, 1, false, 0, Math.PI);
      const m = new THREE.Matrix4();
      if (alongX) m.makeRotationZ(Math.PI / 2);
      else m.makeRotationX(Math.PI / 2).multiply(new THREE.Matrix4().makeRotationY(Math.PI / 2));
      m.setPosition(x, y, z);
      return this.add(g, m, color);
    }
    gable(w, rise, d, x, y, z, color) { // a pitched roof, ridge along x
      const s = new THREE.Shape([new THREE.Vector2(-d / 2, 0), new THREE.Vector2(d / 2, 0), new THREE.Vector2(0, rise)]);
      const g = new THREE.ExtrudeGeometry(s, { depth: w, bevelEnabled: false });
      const m = new THREE.Matrix4().makeRotationY(Math.PI / 2).setPosition(x - w / 2, y, z);
      return this.add(g, m, color);
    }
    hip(w, rise, d, x, y, z, color) { // a roof sloping to all four sides
      const m = new THREE.Matrix4().makeTranslation(x, y + rise / 2, z).multiply(new THREE.Matrix4().makeScale(w, 1, d)).multiply(new THREE.Matrix4().makeRotationY(Math.PI / 4));
      return this.add(new THREE.ConeGeometry(Math.SQRT1_2, rise, 4), m, color);
    }
    arc(r0, r1, h, x, y, z, color, from = 0, sweep = Math.PI, seg = 18) { // part of a ring, as a grandstand is
      const s = new THREE.Shape();
      s.absarc(0, 0, r1, from, from + sweep, false);
      s.absarc(0, 0, r0, from + sweep, from, true);
      const g = new THREE.ExtrudeGeometry(s, { depth: h, bevelEnabled: false, curveSegments: seg });
      g.rotateX(-Math.PI / 2);
      return this.add(g, tmp.clone().makeTranslation(x, y, z), color);
    }
    // A wall with openings cut up from its foot, its face toward +z. openings: [x, width, height, arched = true].
    archWall(w, h, d, openings, x, y, z, color, rotY = 0) {
      const s = new THREE.Shape();
      s.moveTo(-w / 2, 0);
      for (const [ox, ow, oh, round = true] of [...openings].sort((a, b) => a[0] - b[0])) {
        const r = ow / 2;
        s.lineTo(ox - r, 0);
        if (round) { s.lineTo(ox - r, oh - r); s.absarc(ox, oh - r, r, Math.PI, 0, true); } else { s.lineTo(ox - r, oh); s.lineTo(ox + r, oh); }
        s.lineTo(ox + r, 0);
      }
      s.lineTo(w / 2, 0); s.lineTo(w / 2, h); s.lineTo(-w / 2, h); s.lineTo(-w / 2, 0);
      const g = new THREE.ExtrudeGeometry(s, { depth: d, bevelEnabled: false, curveSegments: 10 }).translate(0, 0, -d / 2);
      return this.add(g, new THREE.Matrix4().makeRotationY(rotY).setPosition(x, y, z), color);
    }
    pediment(w, rise, d, x, y, z, color) { // a triangle of stone over a portico, facing +z
      const s = new THREE.Shape([new THREE.Vector2(-w / 2, 0), new THREE.Vector2(w / 2, 0), new THREE.Vector2(0, rise)]);
      return this.add(new THREE.ExtrudeGeometry(s, { depth: d, bevelEnabled: false }), tmp.clone().makeTranslation(x, y, z - d / 2), color);
    }
    lathe(profile, x, y, z, color, seg = 14) { // turned round the upright: profile [[radius, height], ...] from the foot up
      return this.add(new THREE.LatheGeometry(profile.map(([r, h]) => new THREE.Vector2(r, h)), seg), tmp.clone().makeTranslation(x, y, z), color);
    }
    // A box leaning along x, s across for each unit up. from: how far up a leaning tower this piece starts.
    lean(w, h, d, x, y, z, color, s, from = 0) {
      const g = new THREE.BoxGeometry(w, h, d).translate(0, from + h / 2, 0);
      return this.add(g, new THREE.Matrix4().makeTranslation(x, y, z).multiply(new THREE.Matrix4().makeShear(0, 0, s, 0, 0, 0)), color);
    }
    // A rounded rectangle lying flat, outer: [w, d, corner radius], with a hole of inner (or none): a stadium's ring.
    ring(outer, inner, h, x, y, z, color) {
      const s = rounded(new THREE.Shape(), ...outer);
      if (inner) s.holes.push(rounded(new THREE.Path(), ...inner));
      const g = new THREE.ExtrudeGeometry(s, { depth: h, bevelEnabled: false, curveSegments: 6 });
      g.rotateX(-Math.PI / 2);
      return this.add(g, tmp.clone().makeTranslation(x, y, z), color);
    }
    blob(rx, ry, rz, x, y, z, color, turn = 0, tilt = 0) { // a squashed ball, turned and tipped
      const m = new THREE.Matrix4().compose(new THREE.Vector3(x, y, z), new THREE.Quaternion().setFromEuler(new THREE.Euler(0, turn, tilt, "YXZ")), new THREE.Vector3(rx, ry, rz));
      return this.add(new THREE.SphereGeometry(1, 18, 12), m, color);
    }
    geometry() {
      const g = new THREE.BufferGeometry();
      g.setAttribute("position", new THREE.Float32BufferAttribute(this.p, 3));
      g.setAttribute("normal", new THREE.Float32BufferAttribute(this.n, 3));
      g.setAttribute("color", new THREE.Float32BufferAttribute(this.c, 3));
      g.setAttribute("uv", new THREE.Float32BufferAttribute(this.u, 2));
      g.computeBoundingSphere();
      return g;
    }
  }

  function rounded(p, w, d, r) {
    const x = -w / 2, y = -d / 2;
    p.moveTo(x + r, y); p.lineTo(x + w - r, y); p.quadraticCurveTo(x + w, y, x + w, y + r);
    p.lineTo(x + w, y + d - r); p.quadraticCurveTo(x + w, y + d, x + w - r, y + d);
    p.lineTo(x + r, y + d); p.quadraticCurveTo(x, y + d, x, y + d - r);
    p.lineTo(x, y + r); p.quadraticCurveTo(x, y, x + r, y);
    return p;
  }

  // The facades' paint: an atlas of 8 by 8 bays, each a floor high, drawn large enough to stand close to.
  // A column of the atlas is one kind of bay, the same on every floor, so a wall reads in vertical bays as
  // Chamberí's do: a tall window in a stone surround, shutters beside it, an iron balcony before it; and now
  // and then a mirador, the white glazed gallery Madrid hangs on its houses. The paint lies over the wall's
  // own color (paintOver): where the atlas is clear, the wall shows. glow lights some windows at night.
  const BAYS = ["green", "pediment", "brown", "arch", "blind", "plain", "mirador", "pediment"];
  const MIRADOR = BAYS.indexOf("mirador");
  const S = 256; // one bay, in the atlas's pixels
  const OPEN = { l: 86, r: 170, t: 52, b: 232 }; // a window's opening in its bay
  const PANES = { l: 46, r: 210, t: 28, b: 176 }; // a mirador's glass
  const STONEWORK = "#f4efe3", IRONWORK = "#1d2126", BALCONY = "#d3ccbd";

  function windowTextures(anisotropy = 8) {
    const cells = [];
    for (let i = 0; i < 64; i++) cells.push({ lit: Math.random() < 0.46 ? (Math.random() < 0.82 ? "#ffcf7a" : "#cfe6ff") : null, shut: Math.random() < 0.4, curtain: Math.random() < 0.45, plants: Math.random() < 0.3 });
    const make = (paint, ground) => {
      const c = document.createElement("canvas"); c.width = c.height = S * 8;
      const x = c.getContext("2d");
      if (ground) { x.fillStyle = ground; x.fillRect(0, 0, S * 8, S * 8); }
      for (let r = 0; r < 8; r++) for (let q = 0; q < 8; q++) { x.save(); x.translate(q * S, r * S); paint(x, BAYS[q], cells[r * 8 + q]); x.restore(); }
      const t = new THREE.CanvasTexture(c);
      t.wrapS = t.wrapT = THREE.RepeatWrapping; t.encoding = THREE.sRGBEncoding; t.anisotropy = anisotropy;
      return t;
    };
    return { map: make(paintBay), glow: make(paintGlow, "#000") };
  }

  function glass(x, l, t, w, h) {
    const g = x.createLinearGradient(0, t, 0, t + h); g.addColorStop(0, "#27323f"); g.addColorStop(1, "#586c80");
    x.fillStyle = g; x.fillRect(l, t, w, h);
    x.fillStyle = "rgba(255,255,255,.11)"; x.beginPath(); x.moveTo(l, t + h * 0.55); x.lineTo(l + w, t + h * 0.2); x.lineTo(l + w, t + h * 0.42); x.lineTo(l, t + h * 0.77); x.closePath(); x.fill(); // the sky in it
  }

  function paintBay(x, kind, cell) {
    x.fillStyle = "rgba(255,255,255,.5)"; x.fillRect(0, 0, S, 6); // the molding at the floor above, and its shadow
    x.fillStyle = "rgba(0,0,0,.18)"; x.fillRect(0, 6, S, 3);
    if (kind === "mirador") return paintMirador(x);
    const { l, r, t, b } = OPEN, w = r - l, mid = (l + r) / 2;
    // the stone surround, and what crowns it: a lintel, a pediment, an arch
    x.fillStyle = STONEWORK; x.fillRect(l - 12, t - 12, w + 24, b - t + 12);
    if (kind === "arch") { x.beginPath(); x.arc(mid, t + 2, w / 2 + 12, Math.PI, 0); x.fill(); } else x.fillRect(l - 22, t - 24, w + 44, 12);
    if (kind === "pediment") { x.beginPath(); x.moveTo(l - 22, t - 24); x.lineTo(mid, t - 46); x.lineTo(r + 22, t - 24); x.closePath(); x.fill(); }
    x.fillStyle = "rgba(0,0,0,.16)"; x.fillRect(l - 12, b, w + 24, 3);
    // the glass, a curtain behind it, and the woodwork: a frame, two leaves, a transom
    glass(x, l, t, w, b - t);
    if (kind === "arch") { x.save(); x.beginPath(); x.arc(mid, t + 2, w / 2, Math.PI, 0); x.clip(); glass(x, l, t - w / 2, w, w / 2 + 4); x.restore(); }
    if (cell.curtain) { x.fillStyle = "rgba(240,232,214,.62)"; x.fillRect(l, t, w * 0.4, b - t); x.fillRect(r - w * 0.22, t, w * 0.22, b - t); }
    x.fillStyle = "#f3f1ea"; x.fillRect(l, t, w, 4); x.fillRect(l, t, 4, b - t); x.fillRect(r - 4, t, 4, b - t); x.fillRect(mid - 2.5, t, 5, b - t); x.fillRect(l, t + 44, w, 4);
    // shutters folded back against the wall, green or brown; or a blind let half down
    const wood = kind === "green" ? "#3f6b55" : kind === "brown" ? "#6f4d35" : null;
    if (wood) for (const sx of [l - 36, r + 12]) {
      x.fillStyle = wood; x.fillRect(sx, t, 24, b - t - 8);
      x.fillStyle = "rgba(0,0,0,.26)"; for (let y = t + 6; y < b - 14; y += 9) x.fillRect(sx + 3, y, 18, 2.5);
    }
    if (kind === "blind" && cell.shut) {
      x.fillStyle = "#4f7d63"; x.fillRect(l + 4, t + 4, w - 8, (b - t) * 0.62);
      x.fillStyle = "rgba(0,0,0,.22)"; for (let y = t + 10; y < t + (b - t) * 0.62; y += 8) x.fillRect(l + 4, y, w - 8, 2);
    }
    // the balcony: its slab, and the iron: two rails, a bar every hand's breadth
    const bl = l - 36, bw = w + 72;
    x.fillStyle = BALCONY; x.fillRect(bl, 238, bw, 11);
    x.fillStyle = "rgba(0,0,0,.3)"; x.fillRect(bl + 4, 249, bw - 8, 5);
    x.fillStyle = IRONWORK; x.fillRect(bl, 178, bw, 5); x.fillRect(bl, 230, bw, 4);
    for (let bar = bl + 1; bar < bl + bw - 2; bar += 10.4) x.fillRect(bar, 183, 3, 48);
    x.fillRect(bl, 178, 4, 60); x.fillRect(bl + bw - 4, 178, 4, 60);
    if (cell.plants) for (const px of [bl + 18, bl + bw - 18]) { // geraniums
      x.fillStyle = "#b5543a"; x.fillRect(px - 8, 220, 16, 12);
      x.fillStyle = "#4f8a4f"; x.beginPath(); x.arc(px, 212, 12, 0, 7); x.fill();
      x.fillStyle = "#d8433f"; for (const [dx, dy] of [[-5, -4], [4, -7], [1, 3], [-7, 4]]) { x.beginPath(); x.arc(px + dx, 212 + dy, 3, 0, 7); x.fill(); }
    }
  }

  function paintMirador(x) {
    x.fillStyle = "#e3ddd0"; x.fillRect(26, 2, 204, 12); // its little roof
    x.fillStyle = "#f6f3ec"; x.fillRect(34, 12, 188, 236);
    const { l, r, t, b } = PANES, pw = (r - l - 12) / 3, ph = (b - t - 12) / 3;
    for (let i = 0; i < 3; i++) for (let j = 0; j < 3; j++) glass(x, l + i * (pw + 6), t + j * (ph + 6), pw, ph);
    x.fillStyle = "rgba(0,0,0,.10)"; for (let i = 0; i < 3; i++) x.fillRect(l + i * (pw + 6) + 6, b + 16, pw - 12, 42); // the panels under the glass
    x.fillStyle = "#d9d3c5"; x.fillRect(26, 246, 204, 8);
  }

  function paintGlow(x, kind, cell) {
    if (!cell.lit || (kind === "blind" && cell.shut)) return;
    x.fillStyle = cell.lit;
    if (kind === "mirador") { const { l, r, t, b } = PANES; x.fillRect(l, t, r - l, b - t); }
    else { const { l, r, t, b } = OPEN; x.fillRect(l + 4, t + 4, r - l - 8, b - t - 4); }
  }

  // A material whose map is paint over the wall: clear leaves the wall's own color, solid covers it. Without
  // this a map only darkens, and a white mirador on a brick wall would come out brick.
  function paintOver(material) {
    material.onBeforeCompile = (shader) => {
      shader.fragmentShader = shader.fragmentShader
        .replace("#include <map_fragment>", "vec4 facadePaint = vec4( 0.0 );\n#ifdef USE_MAP\n\tfacadePaint = texture2D( map, vUv );\n#endif")
        .replace("#include <color_fragment>", "#include <color_fragment>\n\tdiffuseColor.rgb = mix( diffuseColor.rgb, facadePaint.rgb, facadePaint.a );");
    };
    return material;
  }

  const PAL = { page: "#2fa37a", server: "#3f80cc", kernel: "#d9602d", data: "#a97b4f", tools: "#e68f2c", ext: "#dbb63a" };
  const PALE = "#f2ecde"; // Madrid's limestone: cornices, columns, trim
  const GLASS = "#31465a";
  const TERRACOTTA = "#b9583c", ZINC = "#7f8c97", SLATE = "#56616d", BRICK = "#b4533a", IRON = "#2f5d4a";
  // What Chamberí's streets are made of: brick left bare, or stucco in ochre, cream, salmon and stone, over granite.
  const BRICKS = ["#b5553c", "#bf6040", "#a84c38", "#c26a49"];
  const STUCCOS = ["#e9dcb9", "#e6c78d", "#dfb07c", "#ecc3a6", "#ddd5c4", "#f1e7d0", "#e8d2a6", "#d9c2a0"];
  const FACADES = [...BRICKS, ...STUCCOS];
  const GRANITE = "#b7b1a6", WOODS = ["#5a3d2b", "#2f5d4a", "#7a2f2a", "#22344f", "#3a3a3a"];
  const BASE = 2.7; // a house's ground floor: shops, behind granite
  const AWNINGS = ["#2f6b4f", "#a8322d", "#1f4f8a", "#d9a441", "#f2ecde"];

  // Each draws at the origin, ground at y = 0.
  const DRAW = {
    // The district hall on Plaza de Chamberí: a cupola over the door, columns along the front.
    cityhall(b, c, h) {
      b.box(17, h * 0.55, 13, 0, 0, 0, c, true).box(17.6, 0.5, 13.6, 0, h * 0.55, 0, PALE).box(7, h * 0.24, 7, 0, h * 0.55 + 0.5, 0, shade(c, 1.12), true);
      b.cyl(3.2, 3.4, 1.2, 0, h * 0.79 + 0.5, 0, PALE, 16).dome(3.1, 0, h * 0.79 + 1.7, 0, SLATE, 1.25).cyl(0.12, 0.3, 2.2, 0, h * 0.79 + 5.4, 0, PALE, 6);
      for (const x of [-6, -2, 2, 6]) b.cyl(0.5, 0.5, 4.4, x, 0, 7, PALE, 8);
      b.box(15, 0.5, 2.2, 0, 4.4, 7, PALE);
    },
    // Plaza de Olavide: round, ringed with plane trees, terraces under parasols, a fountain in the middle.
    plaza(b, c) {
      b.cyl(10.4, 10.6, 0.45, 0, 0, 0, "#e7dcc3", 32).cyl(7.4, 7.4, 0.5, 0, 0, 0, "#d9c9a6", 32).cyl(3.6, 4, 0.9, 0, 0.45, 0, PALE, 20).cyl(3.1, 3.1, 0.3, 0, 1.1, 0, "#6fbfdc", 20).cyl(0.35, 0.6, 2.6, 0, 1.1, 0, PALE, 10).ball(0.55, 0, 3.9, 0, c);
      for (let i = 0; i < 10; i++) { const a = (i / 10) * Math.PI * 2; tree(b, Math.cos(a) * 9, 0.45, Math.sin(a) * 9, 1.05); }
      for (let i = 0; i < 6; i++) { const a = (i / 6) * Math.PI * 2 + 0.5; parasol(b, Math.cos(a) * 5.9, 0.5, Math.sin(a) * 5.9, AWNINGS[i % 5]); }
    },
    slab(b, c, h) { b.box(11, h, 10, 0, 0, 0, c, true).box(11.5, 0.4, 10.5, 0, h, 0, PALE).box(8.4, 1.3, 7.4, 0, h + 0.4, 0, TERRACOTTA).box(1.2, 1.6, 1.2, 2.4, h + 1.7, 1.2, BRICK); },
    shop(b, c, h) { b.box(9, h, 8, 0, 0, 0, c, true).box(9.5, 0.35, 8.5, 0, h, 0, PALE).hip(9.4, 1.6, 8.4, 0, h + 0.35, 0, TERRACOTTA).box(9.6, 0.3, 2.2, 0, 2.3, 4.9, AWNINGS[Math.floor(h * 7) % 4]); },
    dome(b, c, h) { b.box(10, h * 0.5, 10, 0, 0, 0, c, true).box(10.6, 0.4, 10.6, 0, h * 0.5, 0, PALE).cyl(4.6, 4.8, 1.1, 0, h * 0.5 + 0.4, 0, PALE, 18).dome(4.4, 0, h * 0.5 + 1.5, 0, shade(c, 1.3)).cyl(0.4, 0.5, 1.2, 0, h * 0.5 + 5.7, 0, PALE, 8); },
    cake(b, c, h) { const n = 5, t = h / n; for (let i = 0; i < n; i++) b.box(15 - i * 2.2, t * 0.85, 15 - i * 2.2, 0, i * t, 0, i % 2 ? PALE : c, i % 2 === 0); },
    // Teatros del Canal: red and black glass boxes, pushed against each other.
    cinema(b, c, h) {
      b.box(9, h, 10, -3.4, 0, -1, "#b3262b").box(8, h * 1.25, 9, 3.8, 0, -0.5, "#23272e").box(15.5, 0.35, 3.2, 0, h * 0.42, 5.4, PALE);
      b.box(6.5, h * 0.5, 0.4, -3.4, h * 0.42, 4.1, "#e04a43").box(5.6, h * 0.8, 0.4, 3.8, h * 0.42, 4.1, GLASS).box(4, 2.5, 0.5, -3.5, 0, 4.2, GLASS).box(4, 2.5, 0.5, 3.5, 0, 4.2, GLASS).box(3, 0.5, 6, 3.8, h * 1.25, -0.5, c);
    },
    hall(b, c, h) { b.box(17, h * 0.55, 12, 0, 0, 0, c, true).box(17.5, 0.35, 12.5, 0, h * 0.55, 0, PALE).barrel(6, 17, 0, h * 0.55 + 0.35, 0, shade(c, 1.25)); },
    warehouse(b, c, h) { b.box(17, h, 12, 0, 0, 0, c).gable(17, 3, 12, 0, h, 0, TERRACOTTA); for (const x of [-5, 0, 5]) b.box(3.2, Math.min(h * 0.65, 4), 0.4, x, 0, 6.1, shade(c, 0.6)); },
    // The Depósito Elevado: the Canal de Isabel II's water tower. Brick, twelve buttresses, a zinc tank and cap.
    watertower(b, c, h) {
      const H = h + 9;
      b.cyl(5.2, 5.8, 1.2, 0, 0, 0, PALE, 12).cyl(3.9, 4.4, H * 0.62, 0, 1.2, 0, BRICK, 12);
      for (let i = 0; i < 12; i++) { const a = (i / 12) * Math.PI * 2; b.box(0.9, H * 0.62, 1.5, Math.cos(a) * 4.7, 1.2, Math.sin(a) * 4.7, shade(BRICK, 0.86), false, -a); }
      b.cyl(5.9, 5.3, 0.7, 0, H * 0.62 + 1.2, 0, PALE, 12).cyl(5.6, 5.6, H * 0.24, 0, H * 0.62 + 1.9, 0, shade(c, 1.05), 24).cyl(5.9, 5.9, 0.4, 0, H * 0.86 + 1.9, 0, PALE, 24);
      b.cyl(0.9, 5.7, H * 0.13, 0, H * 0.86 + 2.3, 0, ZINC, 24).cyl(0.7, 0.9, 1.4, 0, H * 0.99 + 2.3, 0, PALE, 8).cyl(0.05, 0.7, 1.3, 0, H * 0.99 + 3.7, 0, ZINC, 8);
    },
    // Mercado de Chamberí: a brick market hall under two long roofs, striped awnings over the stalls.
    market(b, c, h) {
      b.box(15, h * 0.6, 11, 0, 0, 0, BRICK).box(15.5, 0.35, 11.5, 0, h * 0.6, 0, PALE).gable(15, 2.4, 5.6, 0, h * 0.6 + 0.35, -2.75, shade(c, 1.1)).gable(15, 2.4, 5.6, 0, h * 0.6 + 0.35, 2.75, shade(c, 1.1));
      for (const [i, x] of [-5, 0, 5].entries()) b.box(3.6, 0.3, 2, x, 2.6, 6.4, AWNINGS[i]).box(3, 2.4, 0.4, x, 0, 5.6, GLASS);
    },
    // Estadio de Vallehermoso: the running track, its green, and stands down one side.
    arena(b, c) {
      b.cyl(10.4, 10.6, 0.4, 0, 0, 0, "#c0492f", 30).cyl(7.6, 7.6, 0.45, 0, 0, 0, "#5cae62", 30).arc(10.6, 13.2, 3.2, 0, 0, 0, c, Math.PI * 0.08, Math.PI * 0.84).arc(11.8, 13.2, 4.6, 0, 0, 0, shade(c, 0.8), Math.PI * 0.08, Math.PI * 0.84);
      b.box(9, 3, 0.8, 0, 6.5, -11.4, GLASS);
      for (const x of [-4, 4]) b.box(0.5, 6, 0.5, x, 0.5, -11.4, PALE);
      for (const [x, z] of [[-9.6, 7.4], [9.6, 7.4]]) b.cyl(0.25, 0.4, 11, x, 0, z, PALE, 6).box(2.6, 1.4, 0.5, x, 11, z, "#ffe9a8");
    },
    // Frontón Beti-Jai: the long green wall the ball is played against, and curved galleries facing it.
    arcade(b, c, h) {
      b.box(13, 0.4, 10, 0, 0, 0, "#cfa97a").box(0.8, h + 2, 10, -6.5, 0, 0, "#2f7a55").box(13, h + 2, 0.8, 0, 0, -5, shade("#2f7a55", 0.85));
      for (let i = 0; i < 3; i++) b.arc(4.6, 6.2, 0.5, 2.2, 1.2 + i * 2.5, 0.6, i % 2 ? PALE : c, -Math.PI * 0.42, Math.PI * 0.84);
      for (let i = 0; i < 5; i++) { const a = -Math.PI * 0.42 + (i / 4) * Math.PI * 0.84; b.cyl(0.18, 0.18, 6.4, 2.2 + Math.cos(a) * 6, 0.4, 0.6 - Math.sin(a) * 6, IRON, 6); }
      b.arc(4.4, 6.5, 0.4, 2.2, 6.8, 0.6, TERRACOTTA, -Math.PI * 0.42, Math.PI * 0.84);
    },
    // Museo Sorolla: the painter's house, a tiled roof and tower, a walled garden with its fountain and cypresses.
    villa(b, c, h) {
      b.box(17, 0.3, 14, 0, 0, 0, "#b9cf9a").box(17, 1.4, 0.5, 0, 0.3, 7, PALE).box(0.5, 1.4, 14, -8.5, 0.3, 0, PALE).box(0.5, 1.4, 14, 8.5, 0.3, 0, PALE);
      b.box(11, h * 0.6, 8, 1.5, 0.3, -2.4, c, true).box(11.5, 0.35, 8.5, 1.5, h * 0.6 + 0.3, -2.4, PALE).hip(12, 2.2, 9, 1.5, h * 0.6 + 0.65, -2.4, TERRACOTTA);
      b.box(4, h * 0.95, 4, -4.6, 0.3, -3.6, shade(c, 1.1), true).hip(4.8, 2, 4.8, -4.6, h * 0.95 + 0.3, -3.6, TERRACOTTA);
      b.cyl(1.7, 1.9, 0.6, 2, 0.3, 4.2, PALE, 14).cyl(1.4, 1.4, 0.2, 2, 0.8, 4.2, "#6fbfdc", 14);
      for (const x of [-6.6, -3.4, 6.6]) cypress(b, x, 0.3, 4.6, 1);
      for (const x of [-1.6, 5.6]) b.cyl(0.25, 0.25, 2.6, x, 0.3, 1.9, PALE, 6);
      b.box(8.4, 0.3, 1.2, 2, 2.9, 1.9, shade(TERRACOTTA, 0.9));
    },
    phone(b, c, h) { b.box(9, h, 2.2, 0, 0, 0, "#20262e").box(7.6, h - 2.2, 0.3, 0, 1.1, 1.2, c); for (let i = 0; i < 5; i++) { const x = -14 + i * 7; if (i !== 2) house(b, x, 8, TERRACOTTA); } },
    mast(b, c, h) { b.box(5, 0.5, 5, 0, 0, 0, shade(c, 0.8)).cyl(0.25, 1, h, 0, 0.5, 0, c, 6).dome(2.3, 0, h * 0.7, 0, PALE).cyl(0.9, 0.9, 1.6, 2.2, 0.5, 2, shade(c, 0.7), 10).ball(0.45, 0, h + 0.8, 0, "#e5533d"); },
    portal(b, c, h) { b.box(9, h, 1.5, 0, 0, -4.5, shade(c, 0.8)).box(9, h, 1.5, 0, 0, 4.5, shade(c, 0.8)).box(9, 1.4, 10.5, 0, h, 0, c); },
    // A Madrid terminus: brick wings, an iron and glass shed, the clock tower over the door.
    station(b, c, h) {
      b.box(21, h * 0.5, 15, 0, 0, 0, c, true).box(21.6, 0.4, 15.6, 0, h * 0.5, 0, PALE).barrel(7.5, 21, 0, h * 0.5 + 0.4, 0, shade(c, 1.35));
      b.box(4.5, h * 1.4, 4.5, 8, 0, 5.5, shade(c, 0.9), true).box(5, 0.4, 5, 8, h * 1.4, 5.5, PALE).cyl(1.5, 1.5, 0.3, 8, h * 1.12, 7.8, PALE, 14).hip(5.2, 3.4, 5.2, 8, h * 1.4 + 0.4, 5.5, SLATE);
      for (const x of [-7, -3.5, 0, 3.5]) b.cyl(0.4, 0.4, 3.6, x, 0, 7.8, PALE, 8);
    },
    booth(b, c) { b.box(2.6, 3, 2.6, 0, 0, 0, c).box(3.6, 0.5, 3.6, 0, 3, 0, PALE).box(7, 0.5, 0.5, -3, 1.2, 2.4, "#e5533d"); },
    // The towers along the Paseo de la Castellana.
    tower(b, c, h) { b.box(14, h * 0.5, 14, 0, 0, 0, c, true).box(14.5, 0.4, 14.5, 0, h * 0.5, 0, PALE).box(10.5, h * 0.3, 10.5, 0, h * 0.5 + 0.4, 0, shade(c, 1.1), true).box(6.5, h * 0.2, 6.5, 0, h * 0.8 + 0.4, 0, shade(c, 1.22), true).cyl(0.1, 0.5, 6, 0, h + 0.4, 0, PALE, 6); },
    library(b, c, h) { b.box(19, h * 0.7, 14, 0, 0, 0, c, true).box(21, 1, 16, 0, h * 0.7, 0, PALE).box(13, h * 0.25, 9, 0, h * 0.7 + 1, 0, shade(c, 1.2), true).hip(13.6, 1.8, 9.6, 0, h * 0.95 + 1, 0, SLATE); for (let x = -8; x <= 8; x += 3.2) b.cyl(0.45, 0.45, 5, x, 0, 7.8, PALE, 8); },
    helipad(b, c, h) { b.box(17, h, 15, 0, 0, 0, c, true).box(11, 0.5, 11, 0, h, 0, "#3a424c").cyl(4.2, 4.2, 0.1, 0, h + 0.5, 0, PALE, 24).cyl(3.4, 3.4, 0.12, 0, h + 0.52, 0, "#3a424c", 24); },
    pool(b, c, h) { b.box(11, h, 10, 0, 0, 0, c).box(9, 0.3, 8, 0, h, 0, "#3ec2e6"); },
    gas(b, c) { b.box(9, 0.6, 6, 0, 3.4, 0, c); for (const [x, z] of [[-3.5, -2], [3.5, -2], [-3.5, 2], [3.5, 2]]) b.box(0.4, 3.4, 0.4, x, 0, z, PALE); b.box(1, 1.4, 0.8, -1.5, 0, 0, "#e5533d").box(1, 1.4, 0.8, 1.5, 0, 0, "#e5533d"); },
    court(b, c, h) { b.box(13, h * 0.7, 9, 0, 0, 0, c).gable(13, 2.6, 11, 0, h * 0.7, 0, PALE); for (let x = -5; x <= 5; x += 2.5) b.cyl(0.42, 0.42, h * 0.62, x, 0.3, 5, PALE, 8); },
    // Andén 0: the way down to the old Chamberí station, under the Metro's red diamond.
    entrance(b, c) {
      b.box(6, 0.5, 4, 0, 0, 0, PALE).box(0.3, 2.6, 4, -2.85, 0.5, 0, IRON).box(0.3, 2.6, 4, 2.85, 0.5, 0, IRON).box(6, 0.3, 4.4, 0, 3.1, 0, GLASS).box(4.4, 0.2, 2.4, 0, 0.55, 0.4, "#20262e");
      b.cyl(0.18, 0.22, 9.4, 3.9, 0, 2.6, PALE, 6);
      b.add(new THREE.BoxGeometry(3.3, 3.3, 0.4), new THREE.Matrix4().makeRotationZ(Math.PI / 4).setPosition(3.9, 9.6, 2.6), "#d7282f").box(4.3, 1.05, 0.56, 3.9, 9.08, 2.6, "#1f4f9a");
    },
    // A Madrid Metro platform: white bevelled tile, a dark band, the diamond on the wall.
    rust(b, c) {
      b.box(15, 0.8, 7, 0, 0, 0, shade(c, 0.6)).box(15, 3.4, 0.5, 0, 0.8, -3.2, "#eef1f2").box(15, 0.5, 0.56, 0, 1.2, -3.2, "#1f4f9a").box(12, 0.4, 5, 0, 4.4, 0, c);
      for (const x of [-5, 0, 5]) b.box(0.4, 3.6, 0.4, x, 0.8, 1.6, PALE);
      b.add(new THREE.BoxGeometry(1.5, 1.5, 0.2), new THREE.Matrix4().makeRotationZ(Math.PI / 4).setPosition(0, 2.9, -2.9), "#d7282f").box(2, 0.5, 0.26, 0, 2.65, -2.9, "#1f4f9a");
    },
    terminal(b, c, h) { b.box(21, h * 0.6, 9, 0, 0, 0, c, true).barrel(4.5, 21, 0, h * 0.6, 0, shade(c, 1.3)); },
    atc(b, c, h) { b.cyl(1.5, 2.1, h, 0, 0, 0, c, 10).cyl(3.4, 2.5, 3, 0, h, 0, GLASS, 8).cyl(3.6, 3.6, 0.5, 0, h + 3, 0, PALE, 8); },
    hangar(b, c, h) { b.barrel(h, 20, 0, 0, 0, c, false).box(14, h * 0.75, 0.4, 0, 0, 10.1, shade(c, 0.62)); },
    factory(b, c, h) { b.box(18, h * 0.6, 13, 0, 0, 0, BRICK, true).gable(18, 2.5, 13, 0, h * 0.6, 0, shade(c, 1.1)); for (const x of [-5.5, 0, 5.5]) b.cyl(0.9, 1.25, h + 4, x, 0, -4, shade(BRICK, 0.8), 10).cyl(1.05, 1.05, 0.5, x, h + 4, -4, PALE, 10); },
    pier(b, c, h) { b.box(12, 1, 8, 2, 0, 0, shade(c, 0.6)).box(0.8, h + 8, 0.8, -2, 1, -3, c).box(0.8, h + 8, 0.8, -2, 1, 3, c).box(0.8, h + 8, 0.8, 4, 1, -3, c).box(0.8, h + 8, 0.8, 4, 1, 3, c).box(17, 1, 1.6, 5, h + 8, 0, c).box(2.6, 2, 2.6, 8, 1, 0, "#2b6ea8"); },
    track(b, c) { b.box(19, 0.3, 14, 0, 0, 0, "#3a424c").box(16, 0.4, 3, 0, 0, 0, shade(c, 1.2)).box(8, 2.6, 3, 0, 0, -8.6, c, true); },
    containers(b, c) { const hues = [c, "#2b6ea8", "#c84a3a", "#2f8f6a", "#d8d3c5"]; for (let y = 0; y < 3; y++) for (let i = 0; i < 3 - y; i++) for (let j = 0; j < 2; j++) b.box(6, 2.4, 2.6, -5.5 + i * 6.3 + y * 3, y * 2.5, -1.7 + j * 3.2, hues[(i + y * 2 + j * 3) % 5]); },
    island(b, c, h) { b.cyl(14, 17, 5, 0, -3.5, 0, "#d7c8a2", 14).cyl(11, 13, 0.6, 0, 1.3, 0, "#8fb06e", 14).box(9, h, 9, 0, 1.6, 0, c, true).box(9.5, 0.4, 9.5, 0, h + 1.6, 0, PALE).box(6, h * 0.35, 6, 0, h + 2, 0, shade(c, 1.2), true); tree(b, -8, 1.6, 5, 1); tree(b, 7, 1.6, 7, 0.9); cypress(b, 8, 1.6, -5, 1); },
    lighthouse(b, c, h) { b.box(26, 2.2, 5, -2, -2.2, 0, "#c5c0b2").cyl(2, 3, h, 0, 0, 0, PALE, 12).cyl(2.1, 2.2, 3, 0, h * 0.5, 0, c, 12).cyl(1.5, 1.5, 2.2, 0, h, 0, "#ffd86a", 10).dome(1.9, 0, h + 2.2, 0, "#c84a3a"); },
  };

  // A plane tree, as line Madrid's streets: a pale trunk and a round crown.
  function tree(b, x, y, z, s) {
    b.cyl(0.22 * s, 0.32 * s, 1.7 * s, x, y, z, "#9a8a70", 5);
    b.add(new THREE.IcosahedronGeometry(1.75 * s, 1), tmp.clone().makeTranslation(x, y + 2.9 * s, z), Math.random() < 0.3 ? "#86b46c" : "#6fa568");
  }
  function cypress(b, x, y, z, s) { b.cyl(0.14 * s, 0.2 * s, 0.7 * s, x, y, z, "#7a6650", 5).cyl(0.05, 0.85 * s, 5.2 * s, x, y + 0.7 * s, z, "#3f6f4f", 7); }
  function parasol(b, x, y, z, c) { b.cyl(0.06, 0.06, 1.7, x, y, z, PALE, 5).cyl(0.1, 1.25, 0.5, x, y + 1.7, z, c, 8); }
  function house(b, x, z, roof = TERRACOTTA) { b.box(4, 2.6, 4, x, 0, z, STUCCOS[Math.floor(Math.random() * STUCCOS.length)], { nu: 2 }).hip(4.8, 1.7, 4.8, x, 2.6, z, roof); }

  // One house on the street: granite and shop windows at its foot; floors of windows above, a long balcony on
  // the main floor and one to each window over it; on some, a mirador hung over a bay; then the cornice, and
  // an attic behind a terrace, a mansard with its dormers, or tiles. fronts: which ways its streets lie, each
  // [x, z] one step long. A wall with no street to it gets its windows and nothing more.
  function facade(b, cx, cz, w, d, y, rnd, fronts = []) {
    const floors = 4 + Math.floor(rnd() * 3), h = floors * CELL, top = y + BASE + h;
    const c = rnd() < 0.42 ? BRICKS[Math.floor(rnd() * BRICKS.length)] : STUCCOS[Math.floor(rnd() * STUCCOS.length)];
    b.box(w, BASE, d, cx, y, cz, GRANITE).box(w, h, d, cx, y + BASE, cz, c, { ox: Math.floor(rnd() * 8), oy: Math.floor(rnd() * 8) });
    b.box(w + 0.24, 0.24, d + 0.24, cx, y + BASE - 0.12, cz, PALE); // the band over the shops
    for (const [fx, fz] of fronts) {
      const along = fx ? d : w, bays = Math.max(1, Math.round(along / CELL)), bay = along / bays, half = (fx ? w : d) / 2;
      // A piece on bay k (or across the whole front: k null), its middle standing out from the wall by out.
      const at = (k, out, up, sw, sh, sd, color, win) => {
        const s = k === null ? 0 : -along / 2 + (k + 0.5) * bay;
        b.box(fx ? sd : sw, sh, fx ? sw : sd, cx + fx * (half + out) + (fx ? 0 : s), up, cz + fz * (half + out) + (fz ? 0 : s), color, win);
      };
      // the shops: a window to each bay and a door in the middle, a painted fascia over them, an awning out on some
      const wood = WOODS[Math.floor(rnd() * WOODS.length)];
      for (let k = 0; k < bays; k++) at(k, 0.03, y + 0.35, bay * 0.76, BASE - 1.15, 0.06, bays > 2 && k === Math.floor(bays / 2) ? shade(wood, 0.72) : GLASS);
      at(null, 0.05, y + BASE - 0.74, along * 0.96, 0.44, 0.1, wood);
      if (rnd() < 0.5) at(null, 0.6, y + BASE - 0.88, along * (0.5 + rnd() * 0.4), 0.1, 1.2, AWNINGS[Math.floor(rnd() * AWNINGS.length)]);
      // the main floor's long balcony with its rail, and a balcony to every window above it
      at(null, 0.21, y + BASE, along * 0.95, 0.11, 0.42, BALCONY);
      at(null, 0.4, y + BASE + 0.5, along * 0.95, 0.05, 0.05, IRONWORK);
      for (let f = 1; f < floors; f++) for (let k = 0; k < bays; k++) at(k, 0.19, y + BASE + f * CELL + 0.01, bay * 0.62, 0.1, 0.38, BALCONY);
      // a mirador: white and glazed, hung over an end bay from the main floor up to under the top one
      if (bays >= 2 && rnd() < 0.55) {
        const k = rnd() < 0.5 ? 0 : bays - 1, up = floors - (rnd() < 0.5 ? 1 : 2);
        at(k, 0.25, y + BASE + 0.1, bay * 0.9, up * CELL - 0.1, 0.5, "#f6f3ec", { ox: MIRADOR, oy: Math.floor(rnd() * 8), nu: 1 });
        at(k, 0.27, y + BASE + up * CELL, bay * 0.96, 0.14, 0.58, ZINC);
        at(k, 0.25, y + BASE - 0.08, bay * 0.94, 0.18, 0.52, PALE);
      }
    }
    // the cornice, and the roof
    b.box(w + 0.26, 0.2, d + 0.26, cx, top - 0.2, cz, shade(PALE, 0.9)).box(w + 0.5, 0.22, d + 0.5, cx, top, cz, PALE);
    const roof = rnd();
    if (roof < 0.4) { // an attic set back behind a terrace
      b.box(w - 1.5, CELL, d - 1.5, cx, top + 0.22, cz, shade(c, 1.06), { ox: Math.floor(rnd() * 6), oy: Math.floor(rnd() * 8) }).box(w - 1.2, 0.16, d - 1.2, cx, top + 0.22 + CELL, cz, TERRACOTTA);
    } else if (roof < 0.8) { // a mansard, in slate or zinc, with a dormer over each pair of bays
      b.frustum(w, d, 1.9, 0.7, cx, top + 0.22, cz, rnd() < 0.6 ? SLATE : ZINC);
      for (const [fx, fz] of fronts) {
        const along = fx ? d : w, n = Math.max(1, Math.round(along / 3.4)), half = (fx ? w : d) / 2 - 0.55;
        for (let i = 0; i < n; i++) {
          const s = -along / 2 + ((i + 0.5) * along) / n, px = cx + fx * half + (fx ? 0 : s), pz = cz + fz * half + (fz ? 0 : s);
          b.box(fx ? 0.7 : 0.95, 1.15, fx ? 0.95 : 0.7, px, top + 0.3, pz, PALE).box(fx ? 0.08 : 0.6, 0.75, fx ? 0.6 : 0.08, px + fx * 0.36, top + 0.48, pz + fz * 0.36, GLASS);
        }
      }
    } else b.hip(w + 0.3, 1.5, d + 0.3, cx, top + 0.22, cz, TERRACOTTA); // or tiles
    if (rnd() < 0.7) { // a chimney, capped
      const hx = cx + (rnd() - 0.5) * (w - 2.4), hz = cz + (rnd() - 0.5) * (d - 2.4);
      b.box(0.8, 2.3, 0.8, hx, top + 0.22, hz, BRICK).box(1, 0.15, 1, hx, top + 2.52, hz, PALE);
    }
    return BASE + h;
  }

  // A closed block (a manzana): houses shoulder to shoulder round a courtyard, set back behind a pavement
  // with its trees, and now and then a cupola where two streets meet.
  function manzana(b, x0, x1, z0, z1, y, rnd = Math.random) {
    const inset = 2.1, depth = Math.min(6, (x1 - x0 - 2 * inset) / 3, (z1 - z0 - 2 * inset) / 3);
    const ax = x0 + inset, bx = x1 - inset, az = z0 + inset, bz = z1 - inset;
    const run = (from, to, place) => {
      let a = from;
      while (a < to - 0.5) {
        let len = 5.5 + rnd() * 5.5;
        if (to - (a + len) < 4.5) len = to - a;
        place(a + len / 2, len);
        a += len;
      }
    };
    let top = 0;
    const along = (cz, sign) => run(ax, bx, (cx, len) => { top = Math.max(top, facade(b, cx, cz, len, depth, y, rnd, [[0, sign]])); });
    const across = (cx, sign) => run(az + depth, bz - depth, (cz, len) => { top = Math.max(top, facade(b, cx, cz, depth, len, y, rnd, [[sign, 0]])); });
    along(az + depth / 2, -1); along(bz - depth / 2, 1); across(ax + depth / 2, -1); across(bx - depth / 2, 1);
    // Where two streets meet, on some corners: an eight-sided tower a floor higher than its block, a window to
    // each side, under a slate cupola with its lantern and spire.
    for (const [cx, cz, sx, sz] of [[ax, az, 1, 1], [bx, az, -1, 1], [ax, bz, 1, -1], [bx, bz, -1, -1]]) {
      if (rnd() > 0.5) continue;
      const tx = cx + sx * 1.7, tz = cz + sz * 1.7, h = Math.round((top - BASE) / CELL + 1) * CELL, crown = y + BASE + h;
      b.cyl(2.55, 2.55, BASE, tx, y, tz, GRANITE, 8, false, Math.PI / 8).cyl(2.55, 2.55, h, tx, y + BASE, tz, STUCCOS[Math.floor(rnd() * STUCCOS.length)], 8, true, Math.PI / 8);
      b.cyl(2.7, 2.7, 0.2, tx, y + BASE - 0.1, tz, PALE, 8, false, Math.PI / 8).cyl(2.85, 2.85, 0.28, tx, crown, tz, PALE, 8, false, Math.PI / 8);
      b.dome(2.4, tx, crown + 0.28, tz, rnd() < 0.7 ? SLATE : ZINC, 1.4).cyl(0.5, 0.58, 0.95, tx, crown + 3.5, tz, PALE, 8).dome(0.55, tx, crown + 4.45, tz, SLATE, 1.2).cyl(0.03, 0.12, 1.5, tx, crown + 5.05, tz, SLATE, 5);
    }
    if (bx - ax - 2 * depth > 3) tree(b, (ax + bx) / 2, y, (az + bz) / 2, 0.9); // the courtyard's tree
  }

  // A place is drawn as its landmark (p.look, landmarks.js) where it has one, otherwise by its kind.
  function place(p) {
    const b = new Builder();
    (DRAW[p.look] || DRAW[p.kind] || DRAW.slab)(b, PAL[p.d], p.h);
    return b.geometry();
  }

  // Vehicles point along +x, sitting on y = 0. Toy-sized so they read from far away.
  const VEHICLES = {
    taxi(b, c) { b.box(5, 1.1, 2.3, 0, 0.4, 0, c).box(2.7, 0.95, 2, -0.2, 1.5, 0, GLASS).box(2.6, 0.2, 2, -0.2, 2.45, 0, c).box(0.8, 0.35, 0.8, -0.2, 2.65, 0, "#fff"); },
    bus(b, c) { b.box(9, 2.3, 2.8, 0, 0.4, 0, c).box(8.6, 0.8, 2.9, 0, 1.6, 0, GLASS).box(9, 0.3, 2.8, 0, 2.7, 0, "#fff"); },
    courier(b, c) { b.cyl(0.8, 0.8, 0.2, -1.1, 0, 0, "#2b2f36", 10).cyl(0.8, 0.8, 0.2, 1.1, 0, 0, "#2b2f36", 10).box(2.4, 0.2, 0.3, 0, 0.9, 0, c).cyl(0.45, 0.6, 1.5, -0.2, 1, 0, c, 8).ball(0.5, 0, 3, 0, "#f4d9c0"); },
    foot(b, c) { b.cyl(0.55, 0.7, 2.1, 0, 0, 0, c, 8).ball(0.55, 0, 2.7, 0, "#f4d9c0"); },
    van(b, c) { b.box(5.6, 2.6, 2.5, 0, 0.4, 0, "#fff").box(5.7, 0.7, 2.6, 0, 1, 0, c).box(1.2, 1, 2.3, 2.5, 1.8, 0, GLASS); },
    tram(b, c) { b.box(6, 2.6, 2.6, -3.3, 0.3, 0, c).box(6, 2.6, 2.6, 3.3, 0.3, 0, c).box(12.4, 0.8, 2.7, 0, 1.5, 0, GLASS).box(12.6, 0.3, 2, 0, 2.9, 0, "#fff"); },
    subway(b, c) { for (const x of [-9.4, 0, 9.4]) b.box(9, 2.6, 2.7, x, 0.2, 0, c).box(8.6, 0.8, 2.8, x, 1.4, 0, GLASS); },
    freight(b, c) { b.box(5.5, 3.2, 2.8, 8.4, 0.3, 0, "#3a424c").box(1.6, 1, 2.2, 7, 3.5, 0, c); const h = [c, "#2b6ea8", "#c84a3a"]; for (let i = 0; i < 3; i++) b.box(5.2, 2.7, 2.7, 2.6 - i * 5.7, 0.4, 0, h[i]); },
    forklift(b, c) { b.box(2.8, 1, 2, -0.4, 0.3, 0, c).box(1.5, 1.2, 1.8, -0.8, 1.3, 0, GLASS).box(0.3, 3.2, 1.9, 1.1, 0.2, 0, "#3a424c").box(1.7, 1.4, 1.6, 2.1, 0.5, 0, "#c9a36b"); },
    ship(b, c) { b.box(22, 2.8, 6.4, 0, 0, 0, c).box(5, 5, 5, -8, 2.8, 0, "#fff").box(3.4, 1.5, 5.4, -8, 7.8, 0, GLASS); const h = ["#c84a3a", "#2b6ea8", "#d8b23a", "#2f8f6a"]; for (let i = 0; i < 5; i++) for (let j = 0; j < 2; j++) b.box(2.8, 2.2, 2.5, -2.6 + i * 3.1, 2.8 + (i % 2 ? 2.3 * j : 0), -1.4 + (i % 2 ? 1.4 : j * 2.8), h[(i + j) % 4]); },
    heli(b, c) { b.ball(1.7, 0.6, 1.9, 0, c).box(5, 0.55, 0.55, -3, 2.2, 0, c).box(0.4, 1.8, 0.3, -5.3, 2.3, 0, c).box(4.2, 0.25, 0.25, 0.4, 0, 1.2, "#3a424c").box(4.2, 0.25, 0.25, 0.4, 0, -1.2, "#3a424c").box(0.4, 0.8, 0.4, 0.4, 3.3, 0, "#3a424c"); },
    plane(b, c) { const fuselage = new THREE.CylinderGeometry(1.15, 1.15, 17, 12); b.add(fuselage, new THREE.Matrix4().makeRotationZ(Math.PI / 2), "#fff"); b.box(5, 0.32, 21, 0.5, -0.3, 0, c).box(2.4, 0.26, 7.4, -7.6, 0.3, 0, c).box(2.4, 3.5, 0.3, -7.6, 0.8, 0, c).add(new THREE.SphereGeometry(1.15, 10, 8), new THREE.Matrix4().makeTranslation(8.5, 0, 0), "#fff").box(1.2, 0.6, 2, 7.8, 0.45, 0, GLASS); },
    drone(b, c) { b.box(1.7, 0.55, 1.7, 0, 0.2, 0, c); for (const [x, z] of [[-1.5, -1.5], [1.5, -1.5], [-1.5, 1.5], [1.5, 1.5]]) b.cyl(0.95, 0.95, 0.1, x, 0.75, z, "#d9e4ec", 12).box(0.2, 0.15, 0.2, x, 0.4, z, c); },
    // The sample trip's car: the one a visitor would be driving.
    car(b, c) { b.box(4.6, 1.1, 2.2, 0, 0.45, 0, c).box(2.5, 0.95, 1.9, -0.3, 1.55, 0, GLASS).box(2.4, 0.18, 1.9, -0.3, 2.5, 0, c).box(0.3, 0.4, 1.9, 2.25, 0.8, 0, "#fff6c9").box(0.3, 0.4, 1.9, -2.25, 0.8, 0, "#e5533d"); },
  };

  function vehicle(mode, color) {
    const b = new Builder();
    VEHICLES[mode](b, color);
    return b.geometry();
  }

  function rotor(len) {
    const b = new Builder();
    b.box(len, 0.12, 0.5, 0, 0, 0, "#20262e").box(0.5, 0.12, len, 0, 0, 0, "#20262e");
    return b.geometry();
  }

  return { Builder, DRAW, windowTextures, paintOver, place, vehicle, rotor, tree, cypress, parasol, house, manzana, facade, shade, PAL, PALE, GLASS, GRANITE, TERRACOTTA, ZINC, SLATE, BRICK, IRON, AWNINGS, FACADES };
})();
