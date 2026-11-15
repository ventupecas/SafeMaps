// The landmarks that stand for the app's main parts: Madrid's own, and out on the islands and the ridge to the
// north, some from the rest of Spain. Which place is which landmark is in data.js (LANDMARKS).
// Each draws at the origin, its ground at y = 0, its front toward +z (the side the city is first seen from).
// The landmark keeps its own colors; a flag in the district's color says which part of the app it is.
(() => {
  const { DRAW, shade, tree, PALE, GLASS, GRANITE, SLATE, ZINC, IRON } = MODELS;
  const STONE = "#efe8d9"; // Madrid's white limestone
  const GOLD = "#d8ad3c", BRONZE = "#5b4a33", DARK = "#2a2f36", WATER = "#6fbfdc";
  const M = (x, y, z) => new THREE.Matrix4().makeTranslation(x, y, z);
  const rotX = (a) => new THREE.Matrix4().makeRotationX(a), rotY = (a) => new THREE.Matrix4().makeRotationY(a), rotZ = (a) => new THREE.Matrix4().makeRotationZ(a);

  function flag(b, x, y, z, c, h = 3.2) {
    b.cyl(0.05, 0.07, h, x, y, z, "#ececec", 5).box(1.5, 0.95, 0.06, x + 0.78, y + h - 1.05, z, c).ball(0.1, x, y + h + 0.05, z, GOLD);
  }
  function statue(b, x, y, z, s = 1, color = BRONZE, plinth = GRANITE) {
    b.box(1.1 * s, 1.4 * s, 1.1 * s, x, y, z, plinth).cyl(0.22 * s, 0.32 * s, 1.1 * s, x, y + 1.4 * s, z, color, 7).ball(0.2 * s, x, y + 2.68 * s, z, color);
  }
  // A row of n columns from x0 to x1, standing at y, each with its base and capital.
  function colonnade(b, x0, x1, n, y, z, h, r, color = STONE) {
    for (let i = 0; i < n; i++) {
      const x = n === 1 ? x0 : x0 + ((x1 - x0) * i) / (n - 1);
      b.cyl(r, r * 1.1, h, x, y, z, color, 10).box(r * 2.7, 0.22, r * 2.7, x, y + h - 0.22, z, color).box(r * 2.7, 0.18, r * 2.7, x, y, z, color);
    }
  }
  // n steps rising toward -z, the lowest one's front at z + n * run / 2.
  function steps(b, w, n, x, y, z, color = GRANITE, rise = 0.3, run = 0.5) {
    for (let i = 0; i < n; i++) b.box(w - i * 0.3, rise, (n - i) * run, x, y + i * rise, z - (i * run) / 2, color);
  }
  function lion(b, x, y, z, color = BRONZE) { // lying on its pedestal, looking out
    b.box(1.6, 1.3, 2.6, x, y, z, GRANITE).box(0.9, 0.75, 1.9, x, y + 1.3, z - 0.1, color).ball(0.48, x, y + 2.2, z + 0.75, color);
    for (const s of [-1, 1]) b.box(0.32, 0.5, 0.5, x + s * 0.27, y + 1.3, z + 0.95, color);
  }
  function palm(b, x, y, z, s = 1) {
    b.cyl(0.12 * s, 0.2 * s, 4.4 * s, x, y, z, "#8a7458", 6);
    for (let i = 0; i < 7; i++) b.add(new THREE.BoxGeometry(0.42 * s, 0.06, 2.4 * s), M(x, y + 4.4 * s, z).multiply(rotY((i / 7) * Math.PI * 2)).multiply(rotX(0.42)).multiply(M(0, 0, 1.15 * s)), "#4f8a4a");
  }
  // A clock face on a wall facing +z.
  function clock(b, x, y, z, r = 0.8) {
    b.add(new THREE.CylinderGeometry(r, r, 0.12, 20), rotX(Math.PI / 2).setPosition(x, y, z), "#fbfbf6");
    b.box(0.08, r * 0.62, 0.05, x, y, z + 0.08, DARK).box(r * 0.45, 0.07, 0.05, x + r * 0.2, y, z + 0.08, DARK);
  }
  // One of the Sagrada Família's towers: turned, swelling, a tip of colored mosaic.
  function spire(b, x, y, z, r, h, color, tip) {
    const pts = [];
    for (let i = 0; i <= 12; i++) { const t = i / 12; pts.push([r * (1 - t ** 1.8) ** 0.75 + 0.04, t * h]); }
    pts.push([0, h]);
    b.lathe(pts, x, y, z, color, 12);
    if (tip) b.ball(Math.max(0.28, r * 0.42), x, y + h + 0.1, z, tip);
  }
  // An island out in the water, for the services beyond the computer: sand and grass, its top at y = 1.9.
  function islet(b) { b.cyl(14, 17, 5, 0, -3.5, 0, "#d7c8a2", 14).cyl(11, 13, 0.6, 0, 1.3, 0, "#8fb06e", 14); }

  Object.assign(DRAW, {
    // The Palacio de Cibeles, Madrid's city hall: white stone, a tower over the middle, a spire on every
    // corner, an arcade along the front. And before it, Cybele in her chariot drawn by two lions.
    cibeles(b, c) {
      const z0 = -1.5;
      b.box(16, 1, 12, 0, 0, z0, GRANITE).box(15, 8.5, 10.5, 0, 1, z0, STONE, true).hip(15.4, 2.2, 10.9, 0, 9.5, z0, SLATE);
      for (const sx of [-1, 1]) for (const sz of [-1, 1]) {
        const x = sx * 6.6, z = z0 + sz * 4.4;
        b.box(2.6, 10.5, 2.6, x, 1, z, STONE, { nu: 1 }).box(2.9, 0.3, 2.9, x, 11.5, z, PALE).hip(2.9, 3.4, 2.9, x, 11.8, z, SLATE).cyl(0.03, 0.1, 1.1, x, 15.1, z, GOLD, 5);
      }
      b.box(5, 8, 5, 0, 9.5, z0, STONE, { nu: 3 }).box(5.5, 0.4, 5.5, 0, 17.5, z0, PALE).box(3.6, 3, 3.6, 0, 17.9, z0, STONE, { nu: 2 }).box(4, 0.3, 4, 0, 20.9, z0, PALE);
      b.hip(4, 4.4, 4, 0, 21.2, z0, SLATE).cyl(0.03, 0.16, 2, 0, 25.5, z0, GOLD, 6);
      for (const sx of [-1, 1]) for (const sz of [-1, 1]) b.cyl(0, 0.36, 2.6, sx * 2.45, 17.9, z0 + sz * 2.45, SLATE, 6).ball(0.12, sx * 2.45, 20.6, z0 + sz * 2.45, GOLD);
      clock(b, 0, 19.4, z0 + 1.86, 0.75);
      b.archWall(10.6, 4.4, 1.2, [-4.2, -2.1, 0, 2.1, 4.2].map((x) => [x, 1.5, 3.4]), 0, 1, 4.4, STONE).box(10.8, 0.45, 1.5, 0, 5.4, 4.4, PALE);
      // the fountain
      b.cyl(2.5, 2.7, 0.55, 0, 0, 8.2, GRANITE, 24).cyl(2.25, 2.25, 0.12, 0, 0.48, 8.2, WATER, 24);
      b.box(1.3, 1, 1.6, 0, 0.55, 7.9, PALE).box(0.7, 0.8, 0.7, 0, 1.55, 7.7, PALE).cyl(0.2, 0.32, 1.1, 0, 2.35, 7.7, PALE, 7).ball(0.2, 0, 3.65, 7.7, PALE);
      for (const x of [-0.42, 0.42]) b.box(0.5, 0.7, 1.2, x, 0.55, 9.1, PALE).ball(0.3, x, 1.45, 9.6, PALE);
      flag(b, 4.6, 5.85, 4.6, c, 2.6);
    },
    // The Puerta del Sol: the Real Casa de Correos with its clock, Kilómetro Cero in the pavement before
    // its door, the Bear and the Strawberry Tree, King Carlos III on his horse, two fountains.
    sol(b, c) {
      const R = "#c0603f";
      b.box(21, 0.3, 21, 0, 0, 0, "#ddd5c4");
      b.box(17, 7, 6, 0, 0.3, -6.8, R, true).box(17.4, 0.35, 6.4, 0, 7.3, -6.8, PALE).hip(17.2, 1.8, 6.2, 0, 7.65, -6.8, SLATE);
      b.box(5, 7.6, 6.6, 0, 0.3, -6.6, STONE, { nu: 3 }).box(5.4, 0.35, 7, 0, 7.9, -6.6, PALE);
      b.box(2.6, 2.6, 2.6, 0, 8.25, -6.6, STONE).box(2.9, 0.25, 2.9, 0, 10.85, -6.6, PALE);
      clock(b, 0, 9.55, -5.25, 0.85);
      for (const sx of [-1, 1]) for (const sz of [-1, 1]) b.cyl(0.05, 0.05, 1.5, sx * 0.95, 11.1, -6.6 + sz * 0.95, IRON, 4);
      b.dome(1.1, 0, 12.6, -6.6, IRON, 0.8).ball(0.35, 0, 13.6, -6.6, GOLD); // the ball that drops at New Year
      b.box(1.4, 0.05, 0.9, 0, 0.3, -2.7, "#e9c46a"); // Kilómetro Cero
      // the bear, reaching up into the strawberry tree
      b.box(1.4, 1, 1.4, -5, 0.3, 3, GRANITE).cyl(0.16, 0.24, 1.6, -5, 1.3, 3.35, BRONZE, 6).ball(1.05, -5, 3.4, 3.35, "#4f6f45");
      b.box(0.75, 1.2, 0.6, -5.1, 1.3, 2.75, BRONZE).ball(0.32, -5.1, 2.75, 2.7, BRONZE);
      // Carlos III
      b.box(2.2, 2.3, 1.3, 4.5, 0.3, 2.6, GRANITE).box(1.9, 0.8, 0.65, 4.5, 3.05, 2.6, BRONZE);
      for (const x of [3.8, 5.2]) for (const z of [2.4, 2.8]) b.box(0.18, 0.5, 0.18, x, 2.6, z, BRONZE);
      b.box(0.38, 0.9, 0.38, 5.3, 3.55, 2.6, BRONZE).box(0.7, 0.32, 0.34, 5.6, 4.25, 2.6, BRONZE).cyl(0.18, 0.24, 0.9, 4.4, 3.85, 2.6, BRONZE, 6).ball(0.16, 4.4, 4.9, 2.6, BRONZE);
      for (const x of [-6.6, 6.6]) b.cyl(1.7, 1.8, 0.5, x, 0.3, -1, GRANITE, 18).cyl(1.5, 1.5, 0.1, x, 0.75, -1, WATER, 18).cyl(0.1, 0.2, 1.6, x, 0.3, -1, PALE, 8);
      for (const x of [-2.6, 2.6]) { // street lamps of five lights
        b.cyl(0.08, 0.13, 3.6, x, 0.3, 6.5, IRON, 6).ball(0.22, x, 4.05, 6.5, "#ffe9a8");
        for (let i = 0; i < 4; i++) { const a = (i / 4) * Math.PI * 2; b.ball(0.18, x + Math.cos(a) * 0.5, 3.65, 6.5 + Math.sin(a) * 0.5, "#ffe9a8"); }
      }
      flag(b, -2, 7.65, -3.3, c, 2.4);
    },
    // The Museo del Prado: a long body of brick and stone, a Doric portico in the middle with Velázquez
    // before it, a pavilion at each end, the rotunda's dome behind.
    prado(b, c) {
      const R = "#b9644a", LEAD = "#8f9aa3";
      b.box(17, 0.6, 9, 0, 0, -2, GRANITE).box(16, 5.4, 7.4, 0, 0.6, -2, R);
      for (const x of [-5.1, -3.6, 3.6, 5.1]) { // bays of the front: a door below, a statue in its niche above
        b.box(0.42, 5.4, 0.2, x - 0.75, 0.6, 1.78, STONE).box(0.85, 1.7, 0.1, x, 3.2, 1.76, "#e3dccb").box(0.6, 1.5, 0.12, x, 0.95, 1.76, "#3c3530");
      }
      b.box(16.4, 0.5, 7.8, 0, 6, -2, STONE).hip(16, 1.6, 7.4, 0, 6.5, -2, LEAD);
      for (const sx of [-1, 1]) b.box(3.4, 6.6, 8.6, sx * 7.4, 0.6, -2, STONE, { nu: 2 }).box(3.8, 0.4, 9, sx * 7.4, 7.2, -2, PALE).hip(3.6, 1.4, 8.8, sx * 7.4, 7.6, -2, LEAD);
      b.box(7.2, 0.6, 3.2, 0, 0.6, 3.2, GRANITE);
      colonnade(b, -2.8, 2.8, 6, 1.2, 3.9, 4.4, 0.32);
      b.box(7, 0.8, 2.4, 0, 5.6, 3.3, STONE).box(6.6, 1.3, 1.5, 0, 6.4, 3, STONE).box(4, 0.7, 0.1, 0, 6.7, 3.8, "#d9cfbb");
      b.cyl(2.4, 2.4, 2, 0, 6.5, -3.2, STONE, 18).dome(2.3, 0, 8.5, -3.2, LEAD, 0.6);
      b.box(15, 0.06, 4.6, 0, 0, 7.6, "#a9c58a");
      statue(b, 0, 0, 7.6, 1.2);
      tree(b, -6, 0, 8, 0.9); tree(b, 6, 0, 8, 0.9);
      flag(b, 7.4, 9, -2, c);
    },
    // The Edificio Metrópolis, where Alcalá meets the Gran Vía: two wings and a round corner, columns up
    // its upper floors, a black slate dome trimmed in gold, the winged Victory on top.
    metropolis(b, c) {
      const W = "#f1ead9", BLACK = "#23272d";
      for (const s of [-1, 1]) b.box(9, 9.6, 5.4, s * 4.9, 0, -1.6, W, true, s * 0.42).box(9.3, 0.4, 5.7, s * 4.9, 9.6, -1.6, PALE, false, s * 0.42);
      b.cyl(4, 4, 9.6, 0, 0, 1, W, 16, true).cyl(4.3, 4.3, 0.4, 0, 9.6, 1, PALE, 24);
      for (let i = 0; i < 12; i++) { const a = (i / 12) * Math.PI * 2; b.cyl(0.2, 0.2, 3.2, Math.sin(a) * 4.15, 5.6, 1 + Math.cos(a) * 4.15, PALE, 6); }
      b.cyl(3.4, 3.4, 2.4, 0, 10, 1, W, 16, true).cyl(3.7, 3.7, 0.35, 0, 12.4, 1, PALE, 24);
      for (let i = 0; i < 8; i++) { const a = ((i + 0.5) / 8) * Math.PI * 2; b.cyl(0.15, 0.15, 2.4, Math.sin(a) * 3.55, 10, 1 + Math.cos(a) * 3.55, PALE, 6); }
      b.dome(3.3, 0, 12.75, 1, BLACK, 1.15);
      for (const [r, y] of [[3.32, 12.85], [2.9, 14.55], [1.85, 16]]) b.add(new THREE.TorusGeometry(r, 0.07, 4, 36), rotX(Math.PI / 2).setPosition(0, y, 1), GOLD);
      for (let i = 0; i < 8; i++) { const a = (i / 8) * Math.PI * 2; b.ball(0.2, Math.sin(a) * 2.95, 14.3, 1 + Math.cos(a) * 2.95, GOLD); }
      b.cyl(0.85, 1.1, 1.4, 0, 16.2, 1, BLACK, 12).cyl(0.95, 0.95, 0.15, 0, 17.6, 1, GOLD, 12).cyl(0.3, 0.22, 0.9, 0, 17.75, 1, GOLD, 8);
      b.cyl(0.16, 0.3, 1.3, 0, 18.65, 1, GOLD, 7).ball(0.16, 0, 20.1, 1, GOLD).box(0.08, 0.8, 0.08, 0.22, 20.1, 1.05, GOLD);
      for (const s of [-1, 1]) b.add(new THREE.BoxGeometry(0.9, 1.3, 0.06), rotZ(-s * 0.5).setPosition(s * 0.5, 19.8, 0.85), GOLD);
      flag(b, -7.6, 10, -2.8, c, 2.6);
    },
    // The Edificio Carrión on the Gran Vía, the Capitol cinema's building: a rounded prow in brick and white
    // bands, rising in steps to the sign of red light at its top.
    carrion(b, c) {
      const R = "#a8553f", W = "#efe9dc", RED = "#e2322f";
      for (const s of [-1, 1]) {
        b.box(8.5, 11, 5, s * 4.6, 0, -2, R, true, s * 0.5);
        for (let y = 2.9; y < 11; y += 1.7) b.box(8.7, 0.25, 5.2, s * 4.6, y, -2, W, false, s * 0.5);
        b.box(8.7, 0.4, 5.2, s * 4.6, 11, -2, W, false, s * 0.5);
      }
      b.cyl(3.4, 3.4, 13, 0, 0, 0.8, R, 18, true);
      for (let y = 2.9; y < 13; y += 1.7) b.cyl(3.55, 3.55, 0.25, 0, y, 0.8, W, 24);
      b.cyl(2.7, 2.7, 2.6, 0, 13, 0.8, W, 18, true).cyl(2, 2, 2.4, 0, 15.6, 0.8, W, 18).cyl(1.2, 1.2, 1.2, 0, 18, 0.8, W, 12).cyl(0.05, 0.2, 2, 0, 19.2, 0.8, IRON, 6);
      b.add(new THREE.CylinderGeometry(2.25, 2.25, 1.4, 24, 1, true, -Math.PI * 0.45, Math.PI * 0.9), M(0, 16.9, 0.8), RED);
      b.cyl(3.95, 3.95, 0.55, 0, 2.6, 0.8, RED, 24).box(3.4, 2.5, 0.3, 0, 0, 4.15, GLASS);
      flag(b, 7.2, 11.4, -3.6, c, 2.4);
    },
    // The Mercado de San Miguel: iron painted green and glass all round, a zinc roof, a lantern down its length.
    sanmiguel(b, c) {
      const I = "#2c4f42", G = "#6f8fa0";
      b.box(16, 0.6, 11, 0, 0, 0, GRANITE).box(15, 4.8, 10, 0, 0.6, 0, G);
      for (let i = 0; i <= 8; i++) for (const z of [-5, 5]) b.box(0.3, 4.8, 0.3, -7.5 + (i * 15) / 8, 0.6, z, I);
      for (let i = 1; i < 5; i++) for (const x of [-7.5, 7.5]) b.box(0.3, 4.8, 0.3, x, 0.6, -5 + i * 2, I);
      for (const y of [2.7, 5.1]) b.box(15.4, 0.3, 10.4, 0, y, 0, I);
      b.box(16, 0.4, 11, 0, 5.4, 0, "#55636b").box(15.2, 0.6, 10.2, 0, 5.8, 0, ZINC);
      b.box(13, 1.4, 3.6, 0, 6.4, 0, G).box(13.4, 0.3, 4, 0, 7.8, 0, ZINC);
      for (let i = 0; i < 7; i++) for (const z of [-1.8, 1.8]) b.box(0.2, 1.4, 0.2, -6 + i * 2, 6.4, z, I);
      b.box(2.6, 3.4, 0.2, 0, 0.6, 5.12, "#e9e3d3").box(2, 3, 0.22, 0, 0.6, 5.14, "#3c4a44");
      flag(b, 7.6, 6.2, 5.2, c, 2.4);
    },
    // The Santiago Bernabéu: the pitch, three tiers of stands, a skin of steel ribbons round it all, the roof,
    // and the screen that runs all the way round under it: the scoreboard, in the district's color.
    bernabeu(b, c) {
      b.box(12.2, 0.32, 7.8, 0, 0, 0, "#4f9a52");
      for (let i = 0; i < 6; i++) b.box(2, 0.34, 7.8, -5 + i * 2, 0, 0, i % 2 ? "#5aa75c" : "#4b9150");
      b.box(0.1, 0.36, 7.8, 0, 0, 0, "#f4f4ee").ring([12, 7.6, 0.1], [11.8, 7.4, 0.1], 0.36, 0, 0, 0, "#f4f4ee");
      b.ring([16.4, 12.4, 3.2], [12.6, 8.4, 1.4], 1.6, 0, 0, 0, "#dfe1e4");
      b.ring([18.6, 14.6, 4.2], [16, 12, 3], 4.2, 0, 0, 0, "#c9ccd1");
      b.ring([20.4, 16.4, 5.2], [18.2, 14.2, 4], 6.8, 0, 0, 0, "#dfe1e4");
      b.ring([21, 17, 5.5], [20.4, 16.4, 5.2], 8.4, 0, 0, 0, "#b9bfc6");
      for (const y of [1.6, 3.4, 5.2, 7]) b.ring([21.4, 17.4, 5.7], [20.9, 16.9, 5.4], 0.38, 0, y, 0, "#e8ebef");
      b.ring([21.3, 17.3, 5.6], [14.8, 10.8, 3.2], 0.5, 0, 8.4, 0, "#eef0f2");
      b.ring([15, 11, 3.3], [14.6, 10.6, 3.1], 1.3, 0, 7.1, 0, shade(c, 1.15));
      flag(b, 9.6, 8.9, 7.4, c, 2.4);
    },
    // The Palacio de Cristal in the Retiro, built for an exposition in 1887: a nave and a cross arm in glass
    // and white iron, a dome over the crossing, a portico, and the lake before it.
    cristal(b, c) {
      const G = "#bcd6df", F = "#f4f4f1", T = "#c46e4f";
      b.box(16, 1, 8, 0, 0, -1.5, T).box(7, 1, 12, 0, 0, -1, T);
      b.box(15.4, 3.6, 7.4, 0, 1, -1.5, G).barrel(3.7, 15.4, 0, 4.6, -1.5, G);
      b.box(6.4, 4.6, 11.4, 0, 1, -1, G).barrel(3.2, 11.4, 0, 5.6, -1, G, false);
      for (let x = -7.4; x <= 7.4; x += 1.48) if (Math.abs(x) > 3.3) b.box(0.12, 3.62, 7.5, x, 1, -1.5, F).barrel(3.76, 0.12, x, 4.6, -1.5, F);
      for (let z = -6.3; z <= 4.4; z += 1.5) b.box(6.5, 4.62, 0.12, 0, 1, z, F).barrel(3.26, 0.12, 0, 5.6, z, F, false);
      b.cyl(3, 3, 1.6, 0, 8.6, -1, G, 20).cyl(3.15, 3.15, 0.2, 0, 10.2, -1, F, 24).dome(3, 0, 10.4, -1, G, 1.25);
      for (let i = 0; i < 12; i++) { const a = (i / 12) * Math.PI * 2; b.cyl(0.07, 0.07, 1.6, Math.sin(a) * 3.02, 8.6, -1 + Math.cos(a) * 3.02, F, 4); }
      b.cyl(0.4, 0.5, 1, 0, 14.1, -1, F, 8).ball(0.25, 0, 15.2, -1, F);
      colonnade(b, -1.8, 1.8, 4, 1, 5.6, 3.4, 0.2, F);
      b.box(4.6, 0.4, 1.3, 0, 4.4, 5.6, F).pediment(4.8, 1.3, 1.3, 0, 4.8, 5.6, F);
      b.box(15, 0.16, 3.6, 0, 0, 8.6, "#5fa9c7").box(4, 0.22, 1.2, 0, 0, 6.6, GRANITE).cyl(0.05, 0.14, 2.2, -4.5, 0.16, 8.6, "#e8f6ff", 6);
      flag(b, 7.3, 1, 3, c);
    },
    // Torrespaña, Madrid's television tower, known as El Pirulí: a shaft, the swelling of rooms and dishes
    // near the top, a mast in red and white.
    piruli(b, c) {
      const K = "#e3dfd6";
      b.box(6, 0.5, 6, 0, 0, 0, GRANITE).cyl(2.6, 3, 1.2, 0, 0.5, 0, "#d9d5cc", 16).cyl(1, 1.7, 17, 0, 1.7, 0, K, 16);
      b.cyl(2.2, 1, 1.6, 0, 18.5, 0, K, 20).cyl(2.6, 2.6, 2.4, 0, 20.1, 0, "#d6d2c8", 20).cyl(2.65, 2.65, 0.6, 0, 21, 0, GLASS, 20).cyl(2.8, 2.8, 0.3, 0, 22.5, 0, PALE, 20);
      b.cyl(2.4, 2.4, 1.6, 0, 22.8, 0, "#c9c5bb", 20).cyl(1.4, 2.3, 1.1, 0, 24.4, 0, K, 20);
      for (let i = 0; i < 4; i++) b.cyl(0.4 - i * 0.05, 0.45 - i * 0.05, 1.5, 0, 25.5 + i * 1.5, 0, i % 2 ? "#f2f2f2" : "#d43a2f", 10);
      b.cyl(0.04, 0.15, 2.6, 0, 31.5, 0, "#d43a2f", 6);
      flag(b, 2.3, 0.5, 2.3, c);
    },
    // A windmill of La Mancha, as Consuegra's stand on their ridge: whitewashed, a dark cap, four sails.
    windmill(b, c, h) {
      const H = 5 + h * 0.25, hy = H + 0.95, WOOD = "#5a4a3a";
      b.cyl(2.5, 2.9, 0.5, 0, 0, 0, "#cfc6b0", 12).cyl(1.85, 2.2, H, 0, 0.5, 0, "#f7f5ef", 16);
      b.box(0.9, 1.8, 0.3, 0, 0.5, 2.05, "#2f5d8a").box(0.5, 0.6, 0.2, 0, H * 0.62, 1.93, "#2f5d8a");
      b.cyl(0, 2.15, 2.4, 0, H + 0.5, 0, "#3b3f45", 14);
      b.add(new THREE.CylinderGeometry(0.22, 0.22, 1.2, 8), rotX(Math.PI / 2).setPosition(0, hy, 1.95), WOOD);
      const turn = (h * 0.37) % (Math.PI / 2); // not all stopped at the same angle
      for (let i = 0; i < 4; i++) {
        const at = M(0, hy, 2.5).multiply(rotZ(turn + Math.PI / 4 + (i * Math.PI) / 2));
        b.add(new THREE.BoxGeometry(0.16, 5.8, 0.12), at.clone().multiply(M(0, 3, 0)), WOOD).add(new THREE.BoxGeometry(1.15, 4.3, 0.05), at.clone().multiply(M(0.62, 3.5, 0)), "#ece4d0");
      }
      flag(b, 2.7, 0.5, 1.6, c, 2.6);
    },
    // The old Atocha station: an iron and glass vault between brick walls, a great fan of glass in its gable,
    // a tower either side under a zinc dome, and palms before it (its old shed is a garden of them now).
    atocha(b, c) {
      const R = "#b8583d", VAULT = "#8796a2";
      b.box(15, 3.6, 17, 0, 0, -2, R, { nu: 9 }).barrel(7.5, 17, 0, 3.6, -2, VAULT, false);
      for (let z = -10; z <= 6; z += 2.1) b.barrel(7.6, 0.25, 0, 3.6, z, "#4a5560", false);
      b.box(1.6, 0.55, 17, 0, 10.8, -2, "#6d7c88");
      const gable = new THREE.Shape();
      gable.moveTo(-7.6, 0); gable.lineTo(7.6, 0); gable.lineTo(7.6, 3.6); gable.absarc(0, 3.6, 7.6, 0, Math.PI, false); gable.lineTo(-7.6, 0);
      const hole = new THREE.Path();
      hole.moveTo(-5.6, 3.6); hole.absarc(0, 3.6, 5.6, Math.PI, 0, true); hole.lineTo(-5.6, 3.6);
      gable.holes.push(hole);
      b.add(new THREE.ExtrudeGeometry(gable, { depth: 0.8, bevelEnabled: false, curveSegments: 24 }), M(0, 0, 6.5), R);
      const fan = new THREE.Shape();
      fan.moveTo(-5.6, 3.6); fan.absarc(0, 3.6, 5.6, Math.PI, 0, true); fan.lineTo(-5.6, 3.6);
      b.add(new THREE.ShapeGeometry(fan, 20), M(0, 0, 6.85), "#7fa3b8");
      for (let k = 1; k < 8; k++) b.add(new THREE.BoxGeometry(0.14, 5.6, 0.1), M(0, 3.6, 6.95).multiply(rotZ((k * Math.PI) / 8 - Math.PI / 2)).multiply(M(0, 2.8, 0)), "#3d4852");
      b.add(new THREE.TorusGeometry(3, 0.07, 4, 24, Math.PI), M(0, 3.6, 6.95), "#3d4852");
      b.box(15.6, 0.4, 1.2, 0, 3.4, 6.9, STONE).box(9, 2.8, 0.2, 0, 0, 7.35, "#3b4650");
      for (const x of [-3, 0, 3]) b.box(0.6, 3.2, 0.35, x, 0, 7.45, R);
      for (const sx of [-1, 1]) {
        const x = sx * 8.9;
        b.box(3.4, 11, 3.4, x, 0, 5.4, R, { nu: 2 }).box(3.8, 0.4, 3.8, x, 11, 5.4, STONE);
        for (const qx of [-1.6, 1.6]) for (const qz of [-1.6, 1.6]) b.box(0.36, 11, 0.36, x + qx, 0, 5.4 + qz, STONE);
        b.cyl(1.4, 1.4, 1.2, x, 11.4, 5.4, STONE, 12).dome(1.5, x, 12.6, 5.4, ZINC, 1.3).cyl(0.05, 0.18, 1.4, x, 14.5, 5.4, IRON, 6);
        clock(b, x, 8.8, 7.16, 0.75);
      }
      palm(b, -5, 0, 8.7); palm(b, 5, 0, 8.7);
      flag(b, 1.6, 0, 8.6, c, 4.4);
    },
    // The Puerta de Alcalá: a gate in Madrid's old customs wall, where duties were paid to come into the city.
    // Three arches and two square openings, columns on its faces, the attic over the middle with its figures.
    // It stands beside the road at the canal's edge, facing the traffic.
    alcala(b, c) {
      b.frame = new THREE.Matrix4().makeRotationY(Math.PI / 2).setPosition(-3, 0, -2); // clear of the bridge's traffic
      const G = "#e3ddcf";
      b.box(9, 0.5, 2.2, 0, 0, 0, GRANITE);
      b.archWall(8.4, 4.8, 1.4, [[0, 1.5, 3.2], [-2.3, 1.1, 2.6], [2.3, 1.1, 2.6], [-3.55, 0.55, 2, false], [3.55, 0.55, 2, false]], 0, 0.5, 0, G);
      for (const z of [-0.8, 0.8]) for (const x of [-3.06, -1.25, 1.25, 3.06]) b.cyl(0.16, 0.16, 3.7, x, 0.6, z, STONE, 8).box(0.44, 0.24, 0.44, x, 4.3, z, STONE);
      b.box(8.8, 0.45, 1.8, 0, 5.3, 0, STONE).box(9, 0.25, 1.9, 0, 5.75, 0, STONE);
      b.box(3.4, 1.4, 1.3, 0, 6, 0, STONE).pediment(3.8, 0.9, 1.4, 0, 7.4, 0, STONE).box(1, 0.9, 0.16, 0, 6.2, 0.72, "#d9cdb0");
      for (const x of [-1.45, 1.45]) b.cyl(0.14, 0.2, 0.75, x, 7.4, 0, STONE, 6).ball(0.13, x, 8.25, 0, STONE);
      for (const x of [-3.6, 3.6]) b.box(0.8, 0.9, 1, x, 6, 0, STONE).ball(0.3, x, 7.2, 0, STONE);
      flag(b, 4.7, 0.5, 0.9, c, 2.8);
      b.frame = null;
    },
    // Norman Foster's tower among the Cuatro Torres: glass between two cores, joined at the top by a beam,
    // with the sky showing through under it.
    foster(b, c) {
      const K = "#ece9e1";
      b.box(17, 0.3, 11, 0, 0, 0, GRANITE);
      for (const sx of [-1, 1]) b.box(3, 46, 9, sx * 6.5, 0, 0, K);
      b.box(10, 41, 8.4, 0, 0, 0, "#6f8ea6");
      for (let y = 3.4; y < 41; y += 3.4) b.box(10.1, 0.18, 8.5, 0, y, 0, "#a9bccb");
      for (let x = -4; x <= 4; x += 2) b.box(0.12, 41, 8.5, x, 0, 0, "#8aa3b6");
      b.box(10, 0.6, 8.4, 0, 41, 0, "#55626e").box(16, 3.2, 9.4, 0, 46, 0, K);
      flag(b, 6.5, 49.2, 0, c);
    },
    // The Puerta de Europa: two towers of glass in a dark frame, leaning toward each other over the Castellana.
    kio(b, c) {
      const G = "#4d6478", F = "#2a3138", S = 0.23, H = 22;
      b.box(21, 0.3, 8, 0, 0, 0, GRANITE);
      for (const sx of [-1, 1]) {
        const x = sx * 8.1, s = -sx * S;
        b.lean(4.8, H, 4.8, x, 0, 0, G, s);
        for (let y = 2.2; y < H; y += 2.2) b.lean(5, 0.2, 5, x, 0, 0, F, s, y);
        for (const ex of [-2.4, 2.4]) for (const ez of [-2.4, 2.4]) b.lean(0.32, H, 0.32, x + ex, 0, ez, F, s);
        b.lean(5.2, 0.6, 5.2, x, 0, 0, F, s, H).cyl(1.6, 1.6, 0.12, x + s * (H + 0.6), H + 0.6, 0, "#e9e9e9", 16);
      }
      flag(b, 0, 0.3, 3.4, c);
    },
    // The Biblioteca Nacional: a long front of white stone, steps up to a portico of six columns under its
    // pediment, seated figures either side, slate roofs on the end pavilions.
    biblioteca(b, c) {
      const z0 = -1.5;
      b.box(19, 1.2, 12, 0, 0, z0, GRANITE).box(18, 6.6, 10.5, 0, 1.2, z0, STONE, true).box(18.4, 0.5, 10.9, 0, 7.8, z0, PALE).hip(17.6, 1.6, 10.1, 0, 8.3, z0, "#8d969e");
      for (const sx of [-1, 1]) b.box(3.6, 7.6, 11.2, sx * 7.6, 1.2, z0, STONE, { nu: 2 }).frustum(3.6, 11.2, 2, 0.5, sx * 7.6, 8.8, z0, SLATE);
      b.box(9, 1.2, 2.2, 0, 0, 4.7, GRANITE);
      steps(b, 9.6, 4, 0, 0, 6.6);
      colonnade(b, -3.6, 3.6, 6, 1.2, 5, 5.2, 0.36);
      b.box(8.8, 0.9, 2.6, 0, 6.4, 4.8, STONE).pediment(9.2, 1.9, 2.6, 0, 7.3, 4.8, STONE).box(3.4, 0.9, 0.1, 0, 7.55, 6.15, "#d8cfbb");
      for (const x of [-5.6, 5.6]) statue(b, x, 0, 7.4, 1, "#e6dfcf", STONE);
      flag(b, 0, 9.9, z0, c);
    },
    // The Real Observatorio de Madrid, by Juan de Villanueva: a cross of brick and granite, a portico to the
    // south, and at the top a round temple of columns under its dome.
    observatorio(b, c) {
      const R = "#c98a68";
      b.box(9, 0.6, 9, 0, 0, 0, GRANITE);
      b.box(9, 4, 3.6, 0, 0.6, 0, R, { nu: 4 }).box(3.6, 4, 9, 0, 0.6, 0, R, { nu: 4 }).box(9.3, 0.3, 3.9, 0, 4.6, 0, STONE).box(3.9, 0.3, 9.3, 0, 4.6, 0, STONE);
      colonnade(b, -1.5, 1.5, 4, 0.6, 4.9, 3.5, 0.2);
      b.box(3.8, 0.45, 1.4, 0, 4.1, 4.85, STONE).pediment(3.9, 0.9, 1.4, 0, 4.55, 4.85, STONE);
      b.cyl(1.7, 1.7, 2.4, 0, 4.9, 0, STONE, 16);
      for (let i = 0; i < 12; i++) { const a = (i / 12) * Math.PI * 2; b.cyl(0.11, 0.11, 2.2, Math.sin(a) * 2.2, 4.9, Math.cos(a) * 2.2, STONE, 6); }
      b.cyl(2.45, 2.45, 0.35, 0, 7.1, 0, STONE, 20).dome(2.1, 0, 7.45, 0, "#7e8b96", 0.75).cyl(0.25, 0.3, 0.6, 0, 9, 0, STONE, 8);
      flag(b, 3.6, 4.9, -3.4, c, 2.4);
    },
    // The Congreso de los Diputados: a portico of six columns under its pediment, and the two lions, cast
    // from cannon, that guard its steps.
    congreso(b, c) {
      b.box(14, 0.8, 10, 0, 0, -1, GRANITE).box(13, 6.4, 9, 0, 0.8, -1, "#ebe3d2", true).box(13.4, 0.5, 9.4, 0, 7.2, -1, PALE).box(12.6, 0.6, 8.6, 0, 7.7, -1, "#d9d1c0");
      b.box(7.6, 0.8, 2.4, 0, 0, 4.7, GRANITE);
      steps(b, 8, 3, 0, 0, 6.6, GRANITE, 0.27, 0.5);
      colonnade(b, -3, 3, 6, 0.8, 4.9, 5.4, 0.3, "#efe8d8");
      b.box(7.6, 0.8, 2.4, 0, 6.2, 4.6, STONE).pediment(8, 1.7, 2.4, 0, 7, 4.6, STONE).box(4.2, 0.8, 0.1, 0, 7.25, 5.85, "#d8cfbb");
      lion(b, -4.6, 0, 6.4); lion(b, 4.6, 0, 6.4);
      flag(b, 0, 8.3, -1, c);
    },
    // Barajas Terminal 4: a glass hall under a roof like a wave, bamboo underneath, held up by branching
    // pillars that run through every color from one end of the building to the other.
    barajas(b, c) {
      const HUES = ["#2a5ea8", "#2a8fc4", "#3aa37a", "#8fbf3a", "#f0c419", "#f28c1e", "#e2502f", "#c8323a"];
      b.box(22, 0.3, 10, 0, 0, 0, "#cdd1d4").box(20, 3.6, 7, 0, 0.3, 0, "#8fb2c4");
      for (let i = 0; i < 8; i++) {
        const x = -9.1 + i * 2.6;
        for (const z of [-4.3, 4.3]) {
          b.cyl(0.22, 0.3, 3.4, x, 0.3, z, HUES[i], 8);
          for (const s of [-1, 1]) b.add(new THREE.BoxGeometry(0.16, 1.9, 0.16), M(x + s * 0.5, 4.4, z).multiply(rotZ(-s * 0.55)), HUES[i]);
        }
      }
      const wave = (down) => {
        const g = new THREE.PlaneGeometry(23, 11.6, 46, 1);
        g.rotateX(down ? Math.PI / 2 : -Math.PI / 2);
        const p = g.attributes.position;
        for (let i = 0; i < p.count; i++) p.setY(i, 5.1 + 0.5 * Math.sin((p.getX(i) / 2.6) * Math.PI) - (down ? 0.2 : 0));
        g.computeVertexNormals();
        return g;
      };
      b.add(wave(false), new THREE.Matrix4(), "#e9ecef").add(wave(true), new THREE.Matrix4(), "#d8b07a");
      flag(b, 10.6, 0.3, 4.9, c);
    },
    // The Torre de Hércules in A Coruña, the oldest Roman lighthouse still lit: a square stone tower with the
    // line of its old outside ramp winding up it, two smaller stages, the lantern.
    hercules(b, c) {
      const S = "#cfc4ab";
      b.box(26, 2.2, 5, -2, -2.2, 0, "#bcb6a6").blob(3.6, 1.8, 3.4, 2.5, 0, 0.4, "#9c9585").blob(2.6, 1.4, 2.2, -6, 0, -0.5, "#a49d8c");
      b.box(6.4, 1.2, 6.4, 0, 0, 0, "#b5ab93").box(4.2, 13, 4.2, 0, 1.2, 0, S);
      for (let k = 0; k < 12; k++) {
        const y = 1.8 + 1.05 * k + 0.5;
        b.add(new THREE.BoxGeometry(4.5, 0.26, 0.24), rotY((k % 4) * (Math.PI / 2)).multiply(M(0, y, 2.15)).multiply(rotZ(0.245)), "#e3dac4");
        if (k % 3 === 1) b.add(new THREE.BoxGeometry(0.3, 0.75, 0.1), rotY((k % 4) * (Math.PI / 2)).multiply(M(-0.9, y + 0.9, 2.12)), DARK);
      }
      b.box(4.6, 0.3, 4.6, 0, 14.2, 0, PALE).box(3, 2.4, 3, 0, 14.5, 0, S).box(3.3, 0.25, 3.3, 0, 16.9, 0, PALE);
      b.cyl(1.3, 1.4, 1.8, 0, 17.15, 0, S, 8).cyl(1.05, 1.05, 1.3, 0, 18.95, 0, "#ffd86a", 10).dome(1.2, 0, 20.25, 0, "#8c9a9a").ball(0.18, 0, 21.5, 0, GOLD);
      flag(b, 2.8, 1.2, 2.8, c, 2.6);
    },
    // The Guggenheim Bilbao, on its island: blocks of limestone, a glass atrium, and round them hulls of
    // titanium as if blown by the wind. Before it, a dog made of flowers.
    guggenheim(b, c) {
      islet(b);
      const T = "#cdd2d8", L = "#d9c7a0", Y = 1.9;
      b.box(7, 4.5, 5, -5, Y, -3.5, L).box(5, 6, 4, 5.5, Y, -4.5, L).cyl(1.8, 2.4, 9, 0.5, Y, -0.5, "#8fb4c6", 10);
      // long low hulls along the water, and petals standing up round the atrium, each tipped its own way
      for (const [rx, ry, rz, x, y, z, turn, tilt] of [[6, 1.6, 2.4, -3.5, 3.6, 1.4, 0.35, 0.22], [5.4, 2, 2, 4, 3.4, 1.8, -0.4, -0.25], [6, 1.4, 1.8, 6.4, 2.8, -2.2, 0.8, 0.12], [1.8, 5.2, 1.6, 0.5, 8, -0.6, 0.2, 0.18], [2.4, 3.8, 1.5, -6.4, 4.8, -1.6, -0.5, -0.3], [1.6, 3.4, 2.6, 2.4, 6.6, 2.2, 0.9, 0.4], [1.4, 4.4, 1.2, -1.6, 7.6, 0.6, -0.6, -0.35]]) b.blob(rx, ry, rz, x, y, z, T, turn, tilt);
      b.box(1.6, 10, 2.4, 9, Y, -1, T);
      const P = "#5f9e4f";
      b.blob(1.3, 1.1, 1.9, -5.5, Y + 1.5, 6, P).blob(0.95, 1, 0.95, -5.5, Y + 3.2, 7.2, P).blob(0.5, 0.45, 0.6, -5.5, Y + 3.1, 8.1, P);
      for (const s of [-1, 1]) b.blob(0.3, 0.65, 0.3, -5.5 + s * 0.6, Y + 4.1, 7.1, P).box(0.45, 1, 0.45, -5.5 + s * 0.6, Y, 5.2, P).box(0.45, 1, 0.45, -5.5 + s * 0.6, Y, 6.9, P);
      const blooms = ["#e85d8c", "#f2c94c", "#f07b3f", "#b06ad9", "#f4f1ea"];
      for (let i = 0; i < 22; i++) {
        const a = i * 2.4, u = ((i * 0.37) % 1) * 2 - 1, q = Math.sqrt(1 - u * u);
        b.ball(0.17, -5.5 + Math.cos(a) * q * 1.32, Y + 1.5 + u * 1.1, 6 + Math.sin(a) * q * 1.9, blooms[i % 5]);
      }
      flag(b, 7.2, Y, 6.4, c);
    },
    // The Torre del Oro, on Seville's river: twelve sides in the gold of its old tiles, battlements, a second
    // stage, the turret and its golden dome. Palms on the bank.
    torreoro(b, c) {
      islet(b);
      const Y = 1.9, O = "#d9b26b", O2 = "#e5c78a";
      const crenels = (r, y, n, s) => { for (let i = 0; i < n; i++) { const a = ((i + 0.5) / n) * Math.PI * 2; b.box(s, s * 1.1, s * 0.7, Math.sin(a) * r, y, Math.cos(a) * r, O, false, a); } };
      b.cyl(3.4, 3.6, 8, 0, Y, 0, O, 12);
      crenels(3.25, Y + 8, 12, 0.7);
      for (let i = 0; i < 4; i++) { const a = (i / 4) * Math.PI * 2 + 0.26; b.box(0.3, 0.9, 0.12, Math.sin(a) * 3.47, Y + 4.5, Math.cos(a) * 3.47, DARK, false, a); }
      b.cyl(2.2, 2.2, 3.6, 0, Y + 8, 0, O2, 12);
      for (let i = 0; i < 6; i++) { const a = (i / 6) * Math.PI * 2; b.box(0.36, 0.8, 0.12, Math.sin(a) * 2.24, Y + 9.6, Math.cos(a) * 2.24, DARK, false, a); }
      crenels(2.1, Y + 11.6, 10, 0.45);
      b.cyl(1.25, 1.25, 2, 0, Y + 11.6, 0, O2, 12).dome(1.3, 0, Y + 13.6, 0, "#e9c35a", 1.3).ball(0.22, 0, Y + 15.4, 0, GOLD);
      palm(b, -6, Y, 4.5); palm(b, 5.5, Y, 6); palm(b, -2, Y, 8, 0.85);
      flag(b, 4.4, Y, -4, c);
    },
    // The Aqueduct of Segovia: Roman granite laid without mortar, arches on arches, its piers out in the
    // water. The road data reaches the city over it: an overpass, two thousand years old.
    acueducto(b, c) {
      islet(b);
      b.frame = new THREE.Matrix4().makeRotationY(Math.PI / 2);
      const A = "#bcae94", lower = [], upper = [];
      for (let i = 0; i < 9; i++) lower.push([-12 + i * 3, 2.1, 7.4]);
      for (let i = 0; i < 18; i++) upper.push([-12.75 + i * 1.5, 1.05, 2.6]);
      b.archWall(28.4, 9, 1.9, lower, 0, -0.2, 0, A).box(28.6, 0.25, 2.1, 0, 8.8, 0, shade(A, 1.08));
      b.archWall(27.6, 3.4, 1.5, upper, 0, 9.05, 0, A).box(27.6, 0.5, 1.7, 0, 12.45, 0, shade(A, 1.05));
      b.frame = null;
      flag(b, 4, 1.9, 5, c);
    },
    // The Sagrada Família in Barcelona: the nave and transept in sandstone, the four bell towers of each
    // façade, the Evangelists' four, Mary's with its star, Jesus's tallest of all with its cross. And a crane,
    // as there always has been.
    sagrada(b, c) {
      islet(b);
      const Y = 1.9, S = "#cdb58f", D = "#b39c78", X = 3.5;
      const TIPS = ["#d84b3a", "#e8b939", "#4f8f5a", "#ececec"];
      b.box(4.4, 9, 12, X, Y, -0.5, S).hip(4.6, 3, 12.2, X, Y + 9, -0.5, D).box(11, 8, 3.6, X, Y, 0.5, S).hip(11.2, 2.6, 3.8, X, Y + 8, 0.5, D);
      b.cyl(2.8, 3, 9, X, Y, -6.2, S, 12).dome(2.8, X, Y + 9, -6.2, D, 0.9);
      for (const sx of [-1, 1]) for (let i = 0; i < 4; i++) {
        const z = 0.5 + (i - 1.5) * 1.3, h = i === 0 || i === 3 ? 16.5 : 18.5;
        spire(b, X + sx * 5.6, Y, z, 0.62, h, sx < 0 ? "#bfa985" : S, TIPS[i]);
      }
      b.box(1.6, 7, 4.8, X - 5.6, Y, 0.5, S).box(1.6, 7, 4.8, X + 5.6, Y, 0.5, S);
      for (const [dx, dz] of [[-1.8, -1.3], [1.8, -1.3], [-1.8, 2.3], [1.8, 2.3]]) spire(b, X + dx, Y, 0.5 + dz, 0.95, 23.5, S, "#f2efe6");
      spire(b, X, Y, -4.6, 1.05, 24, S);
      b.ball(0.55, X, Y + 24.3, -4.6, "#f6f3ea");
      for (let i = 0; i < 6; i++) b.add(new THREE.BoxGeometry(0.14, 1.5, 0.14), M(X, Y + 24.3, -4.6).multiply(rotZ((i * Math.PI) / 6)), "#f6f3ea");
      spire(b, X, Y, 0.5, 1.6, 29, S);
      b.box(0.28, 2.6, 0.28, X, Y + 29, 0.5, "#f4f1e8").box(1.6, 0.28, 0.28, X, Y + 30.4, 0.5, "#f4f1e8").box(0.28, 0.28, 1.6, X, Y + 30.4, 0.5, "#f4f1e8");
      b.box(8, 5, 1.4, X, Y, 6, D);
      for (const dx of [-2.6, -0.9, 0.9, 2.6]) b.cyl(0.55, 0.65, 7 + Math.abs(dx), X + dx, Y, 6.2, D, 10);
      const YEL = "#e8b52c";
      b.box(0.55, 33, 0.55, X - 6.5, Y, -4.5, YEL).box(13, 0.45, 0.45, X - 3, Y + 33, -4.5, YEL).box(1.2, 1.2, 1, X - 6.5, Y + 31.8, -4.5, YEL).box(1.4, 0.9, 0.9, X - 9.6, Y + 32.6, -4.5, "#7a7f86");
      b.cyl(0.03, 0.03, 9, X + 2.5, Y + 24, -4.5, DARK, 3);
      flag(b, X - 8.5, Y, 6, c);
    },
  });

  // The Templo de Debod, an Egyptian temple Madrid was given, its gateways standing in a pool: in the park.
  function debod(b, x, y, z) {
    const S = "#d8c9a2";
    b.box(16, 0.2, 14, x, y, z, "#ddd3bb").box(13, 0.12, 6, x, y + 0.2, z + 2.6, "#6fb3cf");
    b.box(6.8, 0.6, 4.6, x, y + 0.2, z - 3.4, shade(S, 0.9)).box(4.8, 3.4, 3.8, x, y + 0.8, z - 3.4, S).box(5.2, 0.5, 4.2, x, y + 4.2, z - 3.4, shade(S, 1.06));
    b.box(1.1, 2, 0.12, x, y + 0.8, z - 1.45, "#3f3a30");
    for (const gz of [z + 1.4, z + 3.8]) for (const gx of [x - 1.2, x + 1.2]) b.box(0.8, 2.9, 0.8, gx, y + 0.2, gz, S);
    for (const gz of [z + 1.4, z + 3.8]) b.box(3.4, 0.6, 1, x, y + 3.1, gz, S);
  }

  MODELS.debod = debod;
  MODELS.palm = palm;
})();
