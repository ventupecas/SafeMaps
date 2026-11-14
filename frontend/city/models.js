// Low-poly models, merged into one vertex-colored geometry each, so the whole city draws in few calls.
// The city is built like Chamberí, in Madrid: closed blocks around courtyards, five to seven floors of
// brick, ochre and cream with a balcony at every window, terracotta and zinc roofs, a cupola on a corner.
THREE.ColorManagement.legacyMode = false; // colors below are written in sRGB; before any is made

window.MODELS = (() => {
  const CELL = 1.7; // one window's size, in world units: a floor
  const col = new THREE.Color();
  const tmp = new THREE.Matrix4();
  const shade = (hex, f) => "#" + new THREE.Color(hex).multiplyScalar(f).getHexString();

  class Builder {
    constructor() { this.p = []; this.n = []; this.c = []; this.u = []; }
    add(geo, matrix, color, win) {
      const g = (geo.index ? geo.toNonIndexed() : geo.clone()).applyMatrix4(matrix);
      const pos = g.attributes.position, nor = g.attributes.normal, uv = g.attributes.uv;
      col.set(color);
      for (let i = 0; i < pos.count; i++) {
        this.p.push(pos.getX(i), pos.getY(i), pos.getZ(i));
        this.n.push(nor.getX(i), nor.getY(i), nor.getZ(i));
        this.c.push(col.r, col.g, col.b);
        if (win) { const [a, b] = win(i, uv.getX(i), uv.getY(i)); this.u.push(a, b); } else this.u.push(0.002, 0.002);
      }
      return this;
    }
    box(w, h, d, x, y, z, color, windows, rotY = 0) {
      const m = new THREE.Matrix4().makeRotationY(rotY).setPosition(x, y + h / 2, z);
      let win = null;
      if (windows) {
        const ox = Math.floor(Math.random() * 8), oy = Math.floor(Math.random() * 8);
        win = (i, u, v) => {
          const face = Math.floor(i / 6); // +x, -x, +y, -y, +z, -z
          if (face === 2 || face === 3) return [0.002, 0.002];
          const across = face < 2 ? d : w;
          const nu = Math.max(1, Math.round(across / CELL)), nv = Math.max(1, Math.round(h / CELL));
          return [(u * nu + ox) / 8, (v * nv + oy) / 8];
        };
      }
      return this.add(new THREE.BoxGeometry(w, h, d), m, color, win);
    }
    cyl(rt, rb, h, x, y, z, color, seg = 14) {
      return this.add(new THREE.CylinderGeometry(rt, rb, h, seg), tmp.clone().makeTranslation(x, y + h / 2, z), color);
    }
    dome(r, x, y, z, color, squash = 1) {
      const m = new THREE.Matrix4().makeScale(1, squash, 1).setPosition(x, y, z);
      return this.add(new THREE.SphereGeometry(r, 16, 8, 0, Math.PI * 2, 0, Math.PI / 2), m, color);
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

  // Walls with Madrid's windows: tall, shuttered, each with its iron balcony, and a line between floors.
  // map darkens the glass by day; emissive lights some windows at night.
  function windowTextures() {
    const make = (paint) => {
      const c = document.createElement("canvas"); c.width = c.height = 512;
      const x = c.getContext("2d");
      paint(x);
      const t = new THREE.CanvasTexture(c);
      t.wrapS = t.wrapT = THREE.RepeatWrapping; t.encoding = THREE.sRGBEncoding; t.anisotropy = 8;
      return t;
    };
    const lit = [];
    for (let i = 0; i < 64; i++) lit.push(Math.random() < 0.46 ? (Math.random() < 0.82 ? "#ffcf7a" : "#cfe6ff") : null);
    const each = (fn) => { for (let r = 0; r < 8; r++) for (let q = 0; q < 8; q++) fn(q * 64, r * 64, lit[r * 8 + q]); };
    return {
      map: make((x) => {
        x.fillStyle = "#fff"; x.fillRect(0, 0, 512, 512);
        each((a, b) => {
          x.fillStyle = "rgba(0,0,0,.10)"; x.fillRect(a, b, 64, 3); // the line of the floor above
          x.fillStyle = "#e9e4da"; x.fillRect(a + 17, b + 9, 30, 46); // the stone surround
          x.fillStyle = "#5d7386"; x.fillRect(a + 21, b + 12, 22, 40); // the glass
          x.fillStyle = "#e9e4da"; x.fillRect(a + 31, b + 12, 2, 40); // its two leaves
          x.fillStyle = "#2c3036"; x.fillRect(a + 12, b + 52, 40, 4); // the balcony's slab ...
          x.fillStyle = "rgba(30,34,40,.55)"; x.fillRect(a + 13, b + 41, 38, 2); // ... and its rail
          for (let k = 0; k < 7; k++) x.fillRect(a + 14 + k * 6, b + 42, 1.5, 10);
        });
      }),
      glow: make((x) => {
        x.fillStyle = "#000"; x.fillRect(0, 0, 512, 512);
        each((a, b, on) => { if (on) { x.fillStyle = on; x.fillRect(a + 21, b + 12, 22, 40); } });
      }),
    };
  }

  const PAL = { page: "#2fa37a", server: "#3f80cc", kernel: "#d9602d", data: "#a97b4f", tools: "#e68f2c", ext: "#dbb63a" };
  const PALE = "#f2ecde"; // Madrid's limestone: cornices, columns, trim
  const GLASS = "#31465a";
  const TERRACOTTA = "#b9583c", ZINC = "#7f8c97", SLATE = "#56616d", BRICK = "#b4533a", IRON = "#2f5d4a";
  // What Chamberí's streets are painted: brick, salmon, ochre, cream, stone.
  const FACADES = ["#b8573d", "#c9694a", "#a94f3b", "#d9a35a", "#e2b872", "#e9cf9a", "#efe0c0", "#e9d3b4", "#dd9872", "#cdbb9c", "#f1e7d3", "#d7c3a0"];
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
    b.add(new THREE.IcosahedronGeometry(1.75 * s, 0), tmp.clone().makeTranslation(x, y + 2.9 * s, z), Math.random() < 0.3 ? "#86b46c" : "#6fa568");
  }
  function cypress(b, x, y, z, s) { b.cyl(0.14 * s, 0.2 * s, 0.7 * s, x, y, z, "#7a6650", 5).cyl(0.05, 0.85 * s, 5.2 * s, x, y + 0.7 * s, z, "#3f6f4f", 7); }
  function parasol(b, x, y, z, c) { b.cyl(0.06, 0.06, 1.7, x, y, z, PALE, 5).cyl(0.1, 1.25, 0.5, x, y + 1.7, z, c, 8); }
  function house(b, x, z, roof = TERRACOTTA) { b.box(4, 2.6, 4, x, 0, z, FACADES[6 + Math.floor(Math.random() * 6)]).hip(4.8, 1.7, 4.8, x, 2.6, z, roof); }

  // One house on the street: its floors, the shop at its foot, a cornice, and an attic or a mansard on top.
  function facade(b, cx, cz, w, d, y, rnd) {
    const floors = 4 + Math.floor(rnd() * 3), h = 2.4 + floors * CELL, c = FACADES[Math.floor(rnd() * FACADES.length)];
    b.box(w, h, d, cx, y, cz, c, true).box(w + 0.24, 2, d + 0.24, cx, y, cz, shade(c, 0.5)).box(w + 0.4, 0.3, d + 0.4, cx, y + h, cz, PALE);
    const roof = rnd();
    if (roof < 0.5) b.box(w - 1.4, 1.2, d - 1.4, cx, y + h + 0.3, cz, TERRACOTTA);
    else if (roof < 0.8) b.box(w - 0.7, 1.5, d - 0.7, cx, y + h + 0.3, cz, rnd() < 0.5 ? ZINC : SLATE);
    if (rnd() < 0.6) b.box(0.8, 1.5, 0.8, cx + (rnd() - 0.5) * (w - 2), y + h + 1.2, cz + (rnd() - 0.5) * (d - 2), BRICK);
    return h;
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
    const along = (cz, sign) => run(ax, bx, (cx, len) => {
      top = Math.max(top, facade(b, cx, cz, len, depth, y, rnd));
      if (rnd() < 0.55) b.box(len * 0.7, 0.22, 1.1, cx, y + 2.05, cz + sign * (depth / 2 + 0.5), AWNINGS[Math.floor(rnd() * AWNINGS.length)]);
    });
    const across = (cx, sign) => run(az + depth, bz - depth, (cz, len) => {
      top = Math.max(top, facade(b, cx, cz, depth, len, y, rnd));
      if (rnd() < 0.55) b.box(1.1, 0.22, len * 0.7, cx + sign * (depth / 2 + 0.5), y + 2.05, cz, AWNINGS[Math.floor(rnd() * AWNINGS.length)]);
    });
    along(az + depth / 2, -1); along(bz - depth / 2, 1); across(ax + depth / 2, -1); across(bx - depth / 2, 1);
    for (const [cx, cz] of [[ax, az], [bx, az], [ax, bz], [bx, bz]]) {
      if (rnd() > 0.3) continue;
      const tx = cx + (cx === ax ? 1 : -1) * 1.6, tz = cz + (cz === az ? 1 : -1) * 1.6, h = top + 1.2;
      b.cyl(2, 2, h, tx, y, tz, FACADES[6 + Math.floor(rnd() * 6)], 10).cyl(2.25, 2.25, 0.3, tx, y + h, tz, PALE, 10).dome(1.95, tx, y + h + 0.3, tz, rnd() < 0.5 ? SLATE : ZINC, 1.35).cyl(0.06, 0.2, 1.5, tx, y + h + 2.9, tz, PALE, 5);
    }
    if (bx - ax - 2 * depth > 3) tree(b, (ax + bx) / 2, y, (az + bz) / 2, 0.9); // the courtyard's tree
  }

  function place(p) {
    const b = new Builder();
    (DRAW[p.kind] || DRAW.slab)(b, PAL[p.d], p.h);
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

  return { Builder, windowTextures, place, vehicle, rotor, tree, cypress, parasol, house, manzana, facade, shade, PAL, PALE, TERRACOTTA, ZINC, SLATE, BRICK, IRON, AWNINGS, FACADES };
})();
