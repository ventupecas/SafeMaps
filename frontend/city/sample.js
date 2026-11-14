// The sample trip: what the planner itself draws, in miniature on Browser Town's streets. A to B, the route it
// recommends in green, the fastest one dashed in gray, and what the fastest one drives through: a left turn
// across traffic at a crash-prone corner, a school zone, a work zone. Each route wears its safety score.
window.SAMPLE = (() => {
  const TRIP = CITY.SAMPLE;
  const GREEN = "#12a15f", GRAY = "#66707c", RED = "#e5484d";
  const group = new THREE.Group();
  const props = new THREE.MeshLambertMaterial({ vertexColors: true });
  const cars = [], rings = [], floats = [];

  // A picture that always faces the viewer, standing on its bottom edge: a pin, a road sign, a score.
  function sign(w, h, size, paint) {
    const c = document.createElement("canvas"); c.width = w; c.height = h;
    paint(c.getContext("2d"), w, h);
    const t = new THREE.CanvasTexture(c); t.encoding = THREE.sRGBEncoding; t.anisotropy = 4;
    const s = new THREE.Sprite(new THREE.SpriteMaterial({ map: t, transparent: true, depthWrite: false }));
    s.center.set(0.5, 0);
    s.scale.set((size * w) / h, size, 1);
    s.renderOrder = 4;
    return s;
  }

  const pin = (letter, color) => sign(160, 220, 15, (x) => {
    x.shadowColor = "rgba(0,0,0,.35)"; x.shadowBlur = 8; x.shadowOffsetY = 3;
    x.fillStyle = color; x.strokeStyle = "#fff"; x.lineWidth = 7;
    x.beginPath(); x.arc(80, 78, 62, Math.PI * 0.82, Math.PI * 0.18, false); x.lineTo(80, 210); x.closePath(); x.fill(); x.stroke();
    x.shadowColor = "transparent";
    x.fillStyle = "#fff"; x.font = "800 78px system-ui, sans-serif"; x.textAlign = "center"; x.textBaseline = "middle"; x.fillText(letter, 80, 82);
  });

  // The safety score as the planner shows it: a ring filled as far as the score, and what the route is.
  const score = (route, color) => sign(260, 300, 15, (x) => {
    x.shadowColor = "rgba(0,0,0,.3)"; x.shadowBlur = 10; x.shadowOffsetY = 3;
    x.fillStyle = "#fff"; x.beginPath(); x.arc(130, 112, 100, 0, 7); x.fill();
    x.shadowColor = "transparent";
    x.lineWidth = 20; x.lineCap = "round";
    x.strokeStyle = "#e3e7ec"; x.beginPath(); x.arc(130, 112, 78, 0, 7); x.stroke();
    x.strokeStyle = color; x.beginPath(); x.arc(130, 112, 78, -Math.PI / 2, -Math.PI / 2 + (route.score / 100) * Math.PI * 2); x.stroke();
    x.fillStyle = "#15191e"; x.font = "800 84px system-ui, sans-serif"; x.textAlign = "center"; x.textBaseline = "middle"; x.fillText(String(route.score), 130, 108);
    x.font = "700 17px system-ui, sans-serif"; x.fillStyle = "#56606d"; x.fillText("SAFETY", 130, 160);
    x.fillStyle = color; x.beginPath(); if (x.roundRect) x.roundRect(20, 228, 220, 56, 28); else x.rect(20, 228, 220, 56); x.fill();
    x.fillStyle = "#fff"; x.font = "800 30px system-ui, sans-serif"; x.fillText(route.label, 130, 258);
  });

  const PAINT = {
    // A left turn across traffic, as the planner marks one: red.
    turn(x) {
      x.fillStyle = RED; x.strokeStyle = "#fff"; x.lineWidth = 9; x.beginPath(); x.arc(90, 90, 80, 0, 7); x.fill(); x.stroke();
      x.strokeStyle = "#fff"; x.lineWidth = 17; x.lineCap = "round"; x.lineJoin = "round";
      x.beginPath(); x.moveTo(112, 140); x.lineTo(112, 84); x.lineTo(58, 84); x.stroke();
      x.fillStyle = "#fff"; x.beginPath(); x.moveTo(34, 84); x.lineTo(70, 54); x.lineTo(70, 114); x.closePath(); x.fill();
    },
    // A school crossing sign: two figures, one small.
    school(x) {
      x.fillStyle = "#cfe41b"; x.strokeStyle = "#16181b"; x.lineWidth = 7; x.lineJoin = "round";
      x.beginPath(); x.moveTo(90, 8); x.lineTo(172, 70); x.lineTo(172, 172); x.lineTo(8, 172); x.lineTo(8, 70); x.closePath(); x.fill(); x.stroke();
      x.fillStyle = "#16181b";
      for (const [cx, top, s] of [[68, 52, 1], [118, 78, 0.78]]) {
        x.beginPath(); x.arc(cx, top + 12 * s, 12 * s, 0, 7); x.fill();
        x.beginPath(); x.moveTo(cx - 17 * s, top + 30 * s); x.lineTo(cx + 17 * s, top + 30 * s); x.lineTo(cx + 9 * s, top + 74 * s); x.lineTo(cx + 15 * s, top + 108 * s);
        x.lineTo(cx + 3 * s, top + 108 * s); x.lineTo(cx, top + 80 * s); x.lineTo(cx - 3 * s, top + 108 * s); x.lineTo(cx - 15 * s, top + 108 * s); x.lineTo(cx - 9 * s, top + 74 * s); x.closePath(); x.fill();
      }
    },
    // A work zone: the orange diamond, and a cone.
    work(x) {
      x.fillStyle = "#f58220"; x.strokeStyle = "#16181b"; x.lineWidth = 7; x.lineJoin = "round";
      x.beginPath(); x.moveTo(90, 6); x.lineTo(174, 90); x.lineTo(90, 174); x.lineTo(6, 90); x.closePath(); x.fill(); x.stroke();
      x.fillStyle = "#16181b"; x.beginPath(); x.moveTo(82, 52); x.lineTo(98, 52); x.lineTo(118, 122); x.lineTo(62, 122); x.closePath(); x.fill();
      x.fillRect(50, 122, 80, 12);
      x.fillStyle = "#f58220"; x.fillRect(68, 80, 44, 9); x.fillRect(62, 102, 56, 9);
    },
  };

  function dashes() {
    const c = document.createElement("canvas"); c.width = 64; c.height = 8;
    const x = c.getContext("2d"); x.fillStyle = "#fff"; x.fillRect(0, 0, 40, 8);
    const t = new THREE.CanvasTexture(c); t.wrapS = THREE.RepeatWrapping; t.encoding = THREE.sRGBEncoding;
    return t;
  }

  function line(path, color, width, lift, map) {
    const mat = new THREE.MeshBasicMaterial({ color, map: map || null, transparent: true, opacity: 0.96, depthWrite: false, side: THREE.DoubleSide });
    const m = new THREE.Mesh(TRAFFIC.strip([path], width, 4.2, lift), mat);
    m.renderOrder = 3; // over the data lanes, which run on these streets too
    return m;
  }

  function car(color, path, speed) {
    const g = new THREE.Group(), body = new THREE.Mesh(MODELS.vehicle("car", color), props);
    body.castShadow = true;
    g.add(body); g.userData.body = body; g.scale.setScalar(1.35);
    cars.push({ g, path, speed, s: 0, wait: 0 });
    return g;
  }

  function build(scene) {
    const y = 0.34, on = (pts) => TRAFFIC.makePath(pts.map(([x, z]) => [x, y, z]), 4.5);
    const safe = on(TRIP.safe.path), fast = on(TRIP.fast.path);
    group.add(line(fast, "#ffffff", 3.6, 0.16), line(fast, GRAY, 2.6, 0.18, dashes()));
    group.add(line(safe, "#ffffff", 4.8, 0.2), line(safe, GREEN, 3.5, 0.22));
    group.add(car(GREEN, safe, 15), car("#8d97a3", fast, 9));

    // What the fastest route drives through, on the street itself.
    const b = new MODELS.Builder();
    for (const h of TRIP.hazards) {
      if (h.kind === "work") { // one lane fenced off: barrier boards, cones, the dug-up strip
        b.box(2.6, 0.06, 15, h.x + 2, 0.02, h.z, "#4a3f33");
        for (let z = -7; z <= 7; z += 2.8) b.cyl(0.07, 0.42, 1.15, h.x + 0.5, 0, h.z + z, "#f58220", 8).cyl(0.26, 0.31, 0.22, h.x + 0.5, 0.5, h.z + z, "#fff", 8);
        for (const z of [-8.4, 8.4]) for (let k = 0; k < 4; k++) b.box(0.8, 0.9, 0.25, h.x + 0.9 + k * 0.8, 0.5, h.z + z, k % 2 ? "#fff" : "#f58220").box(0.14, 0.5, 0.14, h.x + 0.9 + k * 0.8, 0, h.z + z, "#3a424c");
        b.box(1.6, 1.3, 1.1, h.x + 2.3, 0.02, h.z + 2, "#e9b62c").box(0.25, 1.7, 0.25, h.x + 2.3, 1.2, h.z + 1.2, "#e9b62c"); // a digger of sorts
      } else if (h.kind === "school") { // a yellow crossing, and children waiting at the kerb
        for (let k = -3; k <= 3; k++) b.box(0.8, 0.05, 3.6, h.x + k * 1.05, 0.015, h.z, "#f2d13b");
        for (const [dx, c, s] of [[-4.7, "#e5484d", 0.62], [-5.4, "#2f7fe6", 0.5], [4.8, "#f58220", 0.55]]) b.cyl(0.5 * s, 0.65 * s, 2 * s, h.x + dx, 0.25, h.z + 0.6, c, 7).ball(0.5 * s, h.x + dx, 0.25 + 2.5 * s, h.z + 0.6, "#f4d9c0");
      } else { // the corner itself, marked as the planner marks a risky turn
        b.cyl(1.7, 1.7, 0.06, h.x, 0.02, h.z, "#fff", 20).cyl(1.25, 1.25, 0.07, h.x, 0.03, h.z, RED, 20);
        for (let i = 0; i < 2; i++) {
          const r = new THREE.Mesh(new THREE.RingGeometry(0.82, 1, 40), new THREE.MeshBasicMaterial({ color: RED, transparent: true, depthWrite: false, side: THREE.DoubleSide }));
          r.rotation.x = -Math.PI / 2; r.position.set(h.x, 0.45, h.z); r.renderOrder = 3; r.userData.phase = i * 0.5;
          rings.push(r); group.add(r);
        }
      }
      const s = sign(180, 180, 7.5, PAINT[h.kind]);
      s.position.set(h.x, 8.5, h.z); s.userData.y = 8.5; floats.push(s); group.add(s);
      b.cyl(0.1, 0.1, 8.3, h.x + (h.kind === "turn" ? 5.2 : -4.6), 0.25, h.z + (h.kind === "turn" ? 5.2 : -2.4), "#3a424c", 5);
      s.position.x += h.kind === "turn" ? 5.2 : -4.6; s.position.z += h.kind === "turn" ? 5.2 : -2.4;
    }
    const mesh = new THREE.Mesh(b.geometry(), props); mesh.castShadow = true; mesh.receiveShadow = true;
    group.add(mesh);

    const [ax, az] = TRIP.safe.path[0], [bx, bz] = TRIP.safe.path[TRIP.safe.path.length - 1];
    for (const [s, x, z] of [[pin("A", "#0b7a53"), ax - 4.7, az + 4.7], [pin("B", "#c62828"), bx + 4.7, bz - 4.7]]) { s.position.set(x, 0.3, z); s.userData.y = 0.3; floats.push(s); group.add(s); }
    const mid = (route, t) => { const p = new THREE.Vector3(), q = new THREE.Vector3(); route.at(route.len * t, p, q); return p; };
    for (const [s, p] of [[score(TRIP.safe, GREEN), mid(safe, 0.3)], [score(TRIP.fast, GRAY), mid(fast, 0.22)]]) { s.position.set(p.x, 16, p.z); s.userData.y = 16; floats.push(s); group.add(s); }
    scene.add(group);
    for (const c of cars) TRAFFIC.pose(c.g, c.path, 0);
  }

  function update(dt, time) {
    for (const c of cars) {
      if (c.wait > 0) { c.wait -= dt; if (c.wait <= 0) c.s = 0; }
      else { c.s += dt * c.speed; if (c.s >= c.path.len) { c.s = c.path.len; c.wait = 2.5; } }
      TRAFFIC.pose(c.g, c.path, c.s);
    }
    for (const r of rings) { // rings widening from the risky corner
      const k = (time * 0.55 + r.userData.phase) % 1;
      r.scale.setScalar(2 + k * 9); r.material.opacity = 0.75 * (1 - k);
    }
    for (const [i, s] of floats.entries()) s.position.y = s.userData.y + Math.sin(time * 1.6 + i) * 0.35;
  }

  const show = (on) => { group.visible = on; };
  // The corners a camera should frame to see the whole trip.
  const points = () => [...TRIP.safe.path, ...TRIP.fast.path].map(([x, z]) => new THREE.Vector3(x, 0, z));

  return { build, update, show, points };
})();
