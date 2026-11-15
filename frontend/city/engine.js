// Camera, light, sky, labels, picking, focus, the journey player and the showreel. ui.js talks to this through ENGINE.
window.ENGINE = (() => {
  const { PLACES, FLOWS, MODES, DISTRICTS, NAMES } = CITY;
  const reduced = matchMedia("(prefers-reduced-motion: reduce)").matches;
  const listeners = {};
  const emit = (name, data) => (listeners[name] || []).forEach((fn) => fn(data));
  const on = (name, fn) => (listeners[name] = listeners[name] || []).push(fn);

  let wrap, renderer, scene, camera, controls, hemi, sun, ghost, ring, labels = [], labelBox, tip, lampGlow, caption;
  let night = null; // the scene by night (true) or by day (false); null: as the device's own light or dark setting
  let reel = null, idle = 0; // the showreel, while it plays; seconds since anyone touched the page
  let traffic = !reduced, clock = 0, last = 0, frame = 0;
  // Sharpness: the scene is drawn with more pixels than the screen shows (twice as many each way on an ordinary
  // screen), and each screen pixel is the average of several: edges and far windows come out smooth. Where the
  // computer can't keep up it's drawn with fewer, a step at a time; one with no graphics card at all, where a
  // frame takes seconds, goes straight down, to fewer pixels than the screen's. ?res=2 in the address fixes it instead.
  const fixedRes = Number(new URLSearchParams(location.search).get("res")) || 0;
  const MAX_PIXELS = fixedRes ? 4e7 : 6.5e6, LEAST = 0.5;
  let sharpness = fixedRes ? Math.min(fixedRes, 4) : Math.min(3, Math.max(2, (window.devicePixelRatio || 1) * 1.5));
  let timed = 0, span = 0, crawl = 0, resumed = false, software = false;
  // rAF stops while the page is out of sight: the frame it comes back with is no measure of anything.
  document.addEventListener("visibilitychange", () => { resumed = true; });
  // ?show=0 in the address: never start the showreel unasked. ?show=1: start it straight away (a booth screen).
  const wanted = new URLSearchParams(location.search).get("show");
  const attract = wanted !== "0" && !reduced;
  let flight = null, selected = null, hovered = null, litPlaces = null;
  let tour = null, tourCar = null, tourMode = null, payload = null;
  const byId = {};
  const ray = new THREE.Raycaster(), pointer = new THREE.Vector2();
  let pointerSeen = false, downAt = null, lastTap = null, placeMeshes = [], vehicleBodies = [];
  // Zoom: from the whole city down to a balcony. NEAREST: how close the camera comes to what it zooms toward.
  const NEAREST = 1.6, FARTHEST = 1200;
  let zoomGoal = 0, zoomAt = null, aimedAt = null;

  const NOTES = [
    { text: "Browser Town", sub: "runs on the visitor's device", x: -100, y: 8, z: -108, sign: "green" },
    { text: "Server Downtown", sub: "the FastAPI process", x: 118, y: 10, z: -28, sign: "blue" },
    { text: "Build Yard", sub: "commands run by hand", x: 40, y: 6, z: 112, sign: "orange" },
    { text: "Outside Services", sub: "data leaves the computer", x: -20, y: 18, z: -164, sign: "yellow" },
    { text: "The Canal is the network", sub: "crossing it is an HTTP request", x: -18, y: 4, z: -32, sign: "river" },
    { text: "Rust Subway", sub: "backend/rust_kernel", x: 88, y: 0, z: 46, sign: "rust" },
  ];

  // A browser drawing without a graphics card, on the processor (SwiftShader, llvmpipe): there every pixel is
  // costly, and smoothing the edges most of all, so frames take seconds at full sharpness.
  function drawsInSoftware() {
    try {
      const gl = document.createElement("canvas").getContext("webgl");
      if (!gl) return false;
      const info = gl.getExtension("WEBGL_debug_renderer_info");
      const name = String(gl.getParameter(info ? info.UNMASKED_RENDERER_WEBGL : gl.RENDERER));
      const lose = gl.getExtension("WEBGL_lose_context");
      if (lose) lose.loseContext();
      return /swiftshader|llvmpipe|softpipe|software|basic render/i.test(name);
    } catch (error) {
      return false;
    }
  }

  function init(el) {
    wrap = el;
    software = drawsInSoftware();
    if (software && !fixedRes) sharpness = 1; // and no smoothing or shadows: plain, but it keeps up
    try {
      renderer = new THREE.WebGLRenderer({ antialias: !software, powerPreference: "high-performance" });
    } catch (error) {
      return false;
    }
    renderer.setPixelRatio(sharpness);
    renderer.outputEncoding = THREE.sRGBEncoding;
    renderer.shadowMap.enabled = !software;
    renderer.shadowMap.type = THREE.PCFSoftShadowMap;
    renderer.shadowMap.autoUpdate = false;
    wrap.prepend(renderer.domElement);
    renderer.domElement.setAttribute("aria-label", "A 3D model city. The Places tab lists the same places as text.");

    scene = new THREE.Scene();
    camera = new THREE.PerspectiveCamera(30, 1, 5, 3000);
    controls = new THREE.OrbitControls(camera, renderer.domElement);
    controls.enableDamping = true; controls.dampingFactor = 0.08;
    controls.maxPolarAngle = 1.42; controls.minDistance = NEAREST; controls.maxDistance = FARTHEST;
    controls.screenSpacePanning = false;

    hemi = new THREE.HemisphereLight("#ffffff", "#b8bfc6", 0.75);
    sun = new THREE.DirectionalLight("#fff1dc", 0.95);
    sun.position.set(-150, 260, 140); sun.target.position.set(20, 0, -25);
    sun.castShadow = true;
    Object.assign(sun.shadow.camera, { left: -270, right: 270, top: 230, bottom: -230, near: 20, far: 800 });
    sun.shadow.mapSize.set(4096, 4096); sun.shadow.bias = -0.0005; sun.shadow.normalBias = 0.5;
    scene.add(hemi, sun, sun.target);

    ghost = new THREE.MeshLambertMaterial({ color: "#ece9e2" });
    placeMeshes = WORLD.buildAll(scene, renderer.capabilities.getMaxAnisotropy());
    PLACES.forEach((p) => (byId[p.id] = p));
    TRAFFIC.build(scene, byId);
    SAMPLE.build(scene);
    addLampGlow();
    for (const f of FLOWS) if (f.vehicle) { f.vehicle.userData.body.userData.flow = f; vehicleBodies.push(f.vehicle.userData.body); }
    for (const f of FLOWS) { (byId[f.from].flows = byId[f.from].flows || []).push(f); (byId[f.to].flows = byId[f.to].flows || []).push(f); }

    ring = new THREE.Mesh(new THREE.RingGeometry(0.86, 1, 48), new THREE.MeshBasicMaterial({ color: "#ffffff", transparent: true, opacity: 0.9, side: THREE.DoubleSide, depthWrite: false }));
    ring.rotation.x = -Math.PI / 2; ring.visible = false; ring.renderOrder = 3;
    scene.add(ring);

    buildLabels();
    applyTheme();
    matchMedia("(prefers-color-scheme: dark)").addEventListener("change", applyTheme);

    bindPointer();
    bindZoom();
    bindIdle();
    new ResizeObserver(resize).observe(wrap);
    resize();
    overview(true);
    ghostAll(null);
    requestAnimationFrame(loop);
    if (wanted === "1") setTimeout(startReel, 1500);
    return true;
  }

  const isDark = () => (night === null ? matchMedia("(prefers-color-scheme: dark)").matches : night);

  // The sky behind the model: Madrid's pale blue over a warm haze by day, deep blue with a few stars by night.
  const skies = {};
  function sky(dark) {
    const key = dark ? "night" : "day";
    if (skies[key]) return skies[key];
    const c = document.createElement("canvas"); c.width = 512; c.height = 512;
    const x = c.getContext("2d"), g = x.createLinearGradient(0, 0, 0, 512);
    for (const [at, color] of dark ? [[0, "#070d1c"], [0.55, "#111c36"], [1, "#2a3556"]] : [[0, "#a9cfee"], [0.5, "#d3e6f1"], [1, "#f4e7d2"]]) g.addColorStop(at, color);
    x.fillStyle = g; x.fillRect(0, 0, 512, 512);
    if (dark) { x.fillStyle = "rgba(255,255,255,.8)"; for (let i = 0; i < 70; i++) { const r = Math.random() * 0.55 + 0.25; x.beginPath(); x.arc(Math.random() * 512, Math.random() * 230, r, 0, 7); x.fill(); } }
    const t = new THREE.CanvasTexture(c); t.encoding = THREE.sRGBEncoding;
    return (skies[key] = t);
  }

  // Every street lamp as a soft point of light, shown at night.
  function addLampGlow() {
    const c = document.createElement("canvas"); c.width = c.height = 64;
    const x = c.getContext("2d"), g = x.createRadialGradient(32, 32, 0, 32, 32, 32);
    g.addColorStop(0, "rgba(255,240,205,1)"); g.addColorStop(0.18, "rgba(255,214,140,.85)"); g.addColorStop(0.5, "rgba(255,190,100,.28)"); g.addColorStop(1, "rgba(255,180,80,0)");
    x.fillStyle = g; x.fillRect(0, 0, 64, 64);
    const geo = new THREE.BufferGeometry();
    geo.setAttribute("position", new THREE.Float32BufferAttribute(WORLD.lamps.flat(), 3));
    const mat = new THREE.PointsMaterial({ size: 30, map: new THREE.CanvasTexture(c), transparent: true, depthWrite: false, blending: THREE.AdditiveBlending, sizeAttenuation: true });
    // Close up, a lamp's glow would fill the screen: no bigger than a hand's width.
    mat.onBeforeCompile = (shader) => { shader.vertexShader = shader.vertexShader.replace("#include <fog_vertex>", "gl_PointSize = min( gl_PointSize, 160.0 );\n#include <fog_vertex>"); };
    lampGlow = new THREE.Points(geo, mat);
    lampGlow.renderOrder = 5; lampGlow.visible = false;
    scene.add(lampGlow);
  }

  function applyTheme() {
    const dark = isDark();
    const css = getComputedStyle(document.documentElement);
    scene.background = sky(dark);
    hemi.color.set(dark ? "#8aa2cc" : "#ffffff"); hemi.groundColor.set(dark ? "#232c3a" : "#cdbfa8"); hemi.intensity = dark ? 0.64 : 0.72;
    sun.color.set(dark ? "#a9bfe8" : "#ffedd0"); sun.intensity = dark ? 0.34 : 1.0;
    WORLD.water.color.set(dark ? "#14374e" : "#79b7cf");
    WORLD.land.color.set(dark ? "#3d444e" : "#737980");
    WORLD.fabric.emissiveIntensity = dark ? 0.85 : 0;
    if (lampGlow) lampGlow.visible = dark;
    ghost.color.set(dark ? "#77828f" : "#ece9e2");
    for (const p of PLACES) p.mesh.userData.mat.emissiveIntensity = dark ? 1 : 0;
    ring.material.color.set(dark ? "#ffffff" : "#0b7a53");
    TRAFFIC.setTheme(dark ? "night" : "day");
    if (tourCar) tourCar.traverse((o) => { if (o.isMesh) o.material = firstVehicleMat(); });
    renderer.shadowMap.needsUpdate = true;
  }
  const firstVehicleMat = () => FLOWS.find((f) => f.vehicle).vehicle.userData.body.material;

  // ---- Labels
  const shortFile = (p) => p.files.split(",")[0].trim().split("/").pop() || p.files;
  function buildLabels() {
    labelBox = wrap.querySelector(".labels");
    tip = wrap.querySelector(".tip");
    payload = wrap.querySelector(".payload");
    for (const p of PLACES) {
      const el = document.createElement("button");
      el.type = "button"; el.tabIndex = -1;
      el.className = `sign sign-${DISTRICTS[p.d].sign}`;
      el.innerHTML = `<b></b><small></small>`;
      el.firstChild.textContent = p.name; el.lastChild.textContent = shortFile(p);
      el.addEventListener("click", () => select(p.id));
      labelBox.append(el);
      labels.push({ el, place: p, x: p.x, y: p.top + 2, z: p.z, major: p.major, hidden: true });
    }
    for (const n of NOTES) {
      const el = document.createElement("div");
      el.className = `sign note sign-${n.sign}`;
      el.innerHTML = `<b></b><small></small>`;
      el.firstChild.textContent = n.text; el.lastChild.textContent = n.sub;
      labelBox.append(el);
      labels.push({ el, note: true, x: n.x, y: n.y, z: n.z, hidden: true });
    }
    // Street names, on the tiles Madrid writes them on.
    for (const [name, x, z] of NAMES) {
      const el = document.createElement("div");
      el.className = "sign plaque";
      el.textContent = name;
      labelBox.append(el);
      labels.push({ el, street: true, x, y: 1, z, hidden: true });
    }
    for (const l of labels) { l.el.hidden = true; }
  }

  const V = new THREE.Vector3();
  function layoutLabels() {
    const w = wrap.clientWidth, h = wrap.clientHeight, dist = camera.position.distanceTo(controls.target);
    const nearEnough = dist < 330, far = dist > 260;
    const reach = Math.max(160, dist * 5); // zoomed in close, nothing labeled from right across the city
    const scored = [];
    for (const l of labels) {
      const p = l.place;
      let rank = -1;
      if (p !== selected && camera.position.distanceTo(V.set(l.x, l.y, l.z)) > reach) rank = -1;
      else if (l.street) rank = tour || litPlaces || dist > 520 ? -1 : 20;
      else if (l.note) rank = tour || litPlaces ? -1 : far ? 90 : -1;
      else {
        const off = !p || (litPlaces && !litPlaces.has(p.id));
        if (p === selected || p === hovered) rank = 100;
        else if (tour && tour.now && tour.now.has(p.id)) rank = 95;
        else if (off) rank = -1;
        else if (litPlaces) rank = 70;
        else if (p.major) rank = 60;
        else if (nearEnough) rank = 30;
      }
      l.rank = rank;
      if (rank >= 0) scored.push(l);
      else if (!l.hidden) { l.el.hidden = true; l.hidden = true; }
    }
    scored.sort((a, b) => b.rank - a.rank);
    const placed = [];
    for (const l of scored) {
      V.set(l.x, l.y, l.z).project(camera);
      const x = ((V.x + 1) / 2) * w, y = ((1 - V.y) / 2) * h;
      if (l.hidden) { l.el.hidden = false; l.hidden = false; l.bw = l.el.offsetWidth; l.bh = l.el.offsetHeight; }
      const bw = l.bw || 90, bh = l.bh || 30, left = x - bw / 2, top = y - bh - 4;
      const outside = V.z > 1 || left < 4 || top < 4 || left + bw > w - 4 || top + bh > h - 4;
      const hit = placed.some((b) => left < b.left + b.bw + 3 && left + bw + 3 > b.left && top < b.top + b.bh + 2 && top + bh + 2 > b.top);
      if (outside || (hit && l.rank < 100)) { l.el.hidden = true; l.hidden = true; continue; }
      placed.push({ left, top, bw, bh });
      l.el.style.transform = `translate(${Math.round(left)}px, ${Math.round(top)}px)`;
    }
  }

  // ---- Pointer
  function bindPointer() {
    const c = renderer.domElement;
    c.addEventListener("pointermove", (e) => {
      const r = c.getBoundingClientRect();
      pointer.set(((e.clientX - r.left) / r.width) * 2 - 1, -((e.clientY - r.top) / r.height) * 2 + 1);
      pointerSeen = e.pointerType === "mouse";
      tip.style.transform = `translate(${Math.min(e.clientX - r.left + 14, r.width - 250)}px, ${e.clientY - r.top + 14}px)`;
    });
    c.addEventListener("pointerleave", () => { pointerSeen = false; setHover(null, null); });
    c.addEventListener("pointerdown", (e) => { downAt = [e.clientX, e.clientY]; flight = null; });
    c.addEventListener("pointerup", (e) => {
      const moved = !downAt || Math.hypot(e.clientX - downAt[0], e.clientY - downAt[1]) > 6;
      downAt = null;
      if (moved) return;
      // A second tap or click on the same spot, straight after the first: fly in to it.
      const now = performance.now();
      if (lastTap && now - lastTap.t < 350 && Math.hypot(e.clientX - lastTap.x, e.clientY - lastTap.y) < 24) {
        lastTap = null;
        const at = spotUnder(e.clientX, e.clientY);
        if (at) diveTo(at);
        return;
      }
      lastTap = { t: now, x: e.clientX, y: e.clientY };
      const r = c.getBoundingClientRect();
      pointer.set(((e.clientX - r.left) / r.width) * 2 - 1, -((e.clientY - r.top) / r.height) * 2 + 1);
      const hit = pick();
      if (hit && hit.flow) selectFlow(hit.flow.id);
      else if (hit && hit.place) select(hit.place.id);
      else if (!tour) clear();
    });
  }

  function pick() {
    ray.setFromCamera(pointer, camera);
    const vs = vehicleBodies.filter((b) => b.parent.visible);
    const hits = ray.intersectObjects([...vs, ...placeMeshes], false);
    if (!hits.length) return null;
    const u = hits[0].object.userData;
    return u.flow ? { flow: u.flow } : { place: u.place };
  }

  // ---- Zoom
  // The point in the city under a spot on the screen: a building, the street, the water; or out where the
  // ground would be, past the city's edge. null at the sky.
  function spotUnder(cx, cy) {
    const r = renderer.domElement.getBoundingClientRect();
    pointer.set(((cx - r.left) / r.width) * 2 - 1, -((cy - r.top) / r.height) * 2 + 1);
    ray.setFromCamera(pointer, camera);
    const hit = ray.intersectObjects([...placeMeshes, ...WORLD.solids], false)[0];
    if (hit) return hit.point.clone();
    const t = -ray.ray.origin.y / ray.ray.direction.y;
    return t > 0 && t < 4000 ? ray.ray.origin.clone().addScaledVector(ray.ray.direction, t) : null;
  }
  // The camera and the point it turns round, both brought k times as near to at: at stays where it is on the
  // screen, and the view keeps its direction.
  function zoomBy(k, at) {
    const span = camera.position.distanceTo(controls.target), gap = camera.position.distanceTo(at);
    k = Math.min(Math.max(k, NEAREST / span, NEAREST / gap), FARTHEST / span);
    camera.position.sub(at).multiplyScalar(k).add(at);
    controls.target.sub(at).multiplyScalar(k).add(at);
  }
  // Fly in to a spot: it comes to the middle of the screen, seen from the same side, much nearer.
  function diveTo(at, k = 0.32) {
    const off = camera.position.clone().sub(controls.target);
    const len = Math.max(NEAREST * 2.5, Math.min(off.length() * k, 60));
    flyTo({ pos: at.clone().add(off.setLength(len)), target: at }, 0.8);
  }
  // In (dir 1) or out (-1), about the middle of the screen: the + and - buttons and keys.
  function zoom(dir) {
    flight = null; zoomAt = controls.target.clone(); aimedAt = null;
    zoomGoal -= dir * 0.5;
  }
  // The wheel (and a trackpad's pinch) zooms toward whatever is under the pointer. It's caught on its way
  // down to the canvas, before the orbit controls' own wheel zoom, which only goes toward the middle.
  function bindZoom() {
    wrap.addEventListener("wheel", (e) => {
      if (e.target !== renderer.domElement && !(e.target.closest && e.target.closest(".labels"))) return;
      e.preventDefault(); e.stopPropagation();
      flight = null;
      const now = performance.now();
      // where it points, worked out again only when the pointer has moved or the wheel has rested
      if (!aimedAt || now - aimedAt.t > 300 || Math.hypot(e.clientX - aimedAt.x, e.clientY - aimedAt.y) > 6) {
        zoomAt = spotUnder(e.clientX, e.clientY) || controls.target.clone();
        aimedAt = { x: e.clientX, y: e.clientY };
      }
      aimedAt.t = now;
      let px = e.deltaMode === 1 ? e.deltaY * 33 : e.deltaMode === 2 ? e.deltaY * 400 : e.deltaY;
      if (e.ctrlKey) px *= 3; // a pinch on a trackpad comes in small steps
      zoomGoal += Math.max(-250, Math.min(250, px)) * 0.0016;
    }, { capture: true, passive: false });
  }

  function setHover(place, flow) {
    if (hovered !== place) {
      if (hovered) hovered.mesh.userData.mat.color.setScalar(1);
      hovered = place;
      if (hovered) hovered.mesh.userData.mat.color.setScalar(1.28);
    }
    renderer.domElement.style.cursor = place || flow ? "pointer" : "";
    if (place || flow) {
      tip.hidden = false;
      const m = flow && MODES[flow.mode];
      tip.firstChild.textContent = flow ? `${m.name}: ${flow.label}` : place.name;
      tip.lastChild.textContent = flow ? `${byId[flow.from].name} to ${byId[flow.to].name}` : place.files;
    } else tip.hidden = true;
  }

  // ---- Focus
  function ghostAll(keep) {
    litPlaces = keep;
    for (const p of PLACES) {
      const running = !p.flows || p.flows.some((f) => TRAFFIC.running(f) && !f.idle);
      const lit = running && (!keep || keep.has(p.id));
      p.mesh.material = lit ? p.mesh.userData.mat : ghost;
    }
  }
  function reveal(on) {
    for (const m of [WORLD.land, WORLD.fabric]) {
      if (m.transparent === on) continue;
      m.transparent = on; m.opacity = on ? 0.3 : 1; m.depthWrite = !on; m.needsUpdate = true;
    }
  }
  const endpoints = (flows) => { const s = new Set(); for (const f of flows) { s.add(f.from); s.add(f.to); } return s; };

  function select(id) {
    if (tour) stopTour();
    const p = byId[id];
    selected = p;
    const flows = (p.flows || []).filter(TRAFFIC.running);
    TRAFFIC.setFocus({ flows: new Set(flows.map((f) => f.id)) });
    const keep = endpoints(flows); keep.add(id); ghostAll(keep);
    reveal(p.d === "kernel"); SAMPLE.show(false);
    ring.visible = true; ring.position.set(p.x, p.mesh.position.y + 0.4, p.z);
    p.ringSize = p.kind === "island" ? 22 : 15; ring.scale.setScalar(p.ringSize);
    frameBox(pointsOf([p]), 90);
    emit("focus", { place: p });
  }

  function selectMode(mode) {
    if (tour) stopTour();
    selected = null; ring.visible = false;
    const flows = FLOWS.filter((f) => f.mode === mode && TRAFFIC.running(f));
    TRAFFIC.setFocus({ flows: new Set(flows.map((f) => f.id)) });
    const ends = endpoints(flows);
    ghostAll(ends); SAMPLE.show(false);
    reveal(mode === "subway");
    if (mode === "subway") frameBox(pointsOf([...ends].map((id) => byId[id])), 200);
    emit("focus", { mode });
  }

  function selectFlow(id) {
    if (tour) stopTour();
    const f = TRAFFIC.flows[id];
    selected = null; ring.visible = false;
    TRAFFIC.setFocus({ flows: new Set([id]) });
    ghostAll(endpoints([f])); SAMPLE.show(false);
    reveal(f.mode === "subway");
    frameBox(f.out.pts, 70);
    emit("focus", { flow: f });
  }

  function clear(fly) {
    if (tour) stopTour();
    selected = null; ring.visible = false;
    TRAFFIC.setFocus(null); ghostAll(null); reveal(false); SAMPLE.show(true);
    if (fly) overview();
    emit("focus", {});
  }

  function setMap(m) {
    TRAFFIC.setMap(m);
    ghostAll(litPlaces);
    emit("map", m);
  }

  // ---- Camera
  function resize() {
    const w = wrap.clientWidth, h = wrap.clientHeight;
    if (!w || !h) return;
    // No more pixels than a graphics card of the usual kind fills smoothly, however large the window.
    renderer.setPixelRatio(Math.min(sharpness, Math.sqrt(MAX_PIXELS / (w * h))));
    renderer.setSize(w, h, false);
    renderer.domElement.style.width = w + "px"; renderer.domElement.style.height = h + "px";
    camera.aspect = w / h; camera.updateProjectionMatrix();
    camera.updateMatrixWorld();
    for (const l of labels) { l.bw = 0; l.hidden || (l.el.hidden = true); l.hidden = true; }
    if (controls) layoutLabels();
  }

  function view(target, dist, polar = 0.9, azimuth) {
    const off = camera.position.clone().sub(controls.target);
    const az = azimuth === undefined ? Math.atan2(off.x, off.z) : azimuth;
    const pos = new THREE.Vector3(Math.sin(polar) * Math.sin(az), Math.cos(polar), Math.sin(polar) * Math.cos(az)).multiplyScalar(dist).add(target);
    return { pos, target };
  }
  function flyTo(v, seconds = 1.15) {
    if (reduced || seconds === 0) { camera.position.copy(v.pos); controls.target.copy(v.target); controls.update(); flight = null; return; }
    flight = { t: 0, seconds, fromPos: camera.position.clone(), fromTarget: controls.target.clone(), ...v };
  }
  const overviewDist = () => Math.min(900, 250 / (Math.tan((15 * Math.PI) / 180) * Math.max(camera.aspect, 0.8)));
  function overview(instant) {
    flyTo(view(new THREE.Vector3(20, 0, -22), overviewDist(), 0.84, 0.08), instant ? 0 : 1.4);
  }

  // The sample trip, close up: what the planner draws, and why the green route is the one it recommends.
  function showSample() {
    clear();
    const box = new THREE.Box3().setFromPoints(SAMPLE.points()), c = box.getCenter(new THREE.Vector3());
    flyTo(view(c, Math.min(520, 150 / Math.min(1.1, camera.aspect) + 120), 0.82, 0.45));
    emit("focus", { sample: true });
  }

  // ---- The showreel: with nobody at the controls, the city shows itself, one shot after another, by day and
  // then by night. Any touch, click or key hands the controls back.
  const IDLE_S = 75;
  const REEL = [
    { title: "Safe Route City", text: "Every part of the Safe Route Planner, built as Chamberí, Madrid.", whole: true, secs: 11 },
    { title: "Safety first", text: "Green is the route the planner recommends. Gray is the fastest: a left turn across traffic, a school zone, a work zone.", sample: true, secs: 14 },
    { title: "Puerta del Sol", text: "Browser Town: the page itself, running on the visitor's device.", at: "core", dist: 150, az: -0.5, secs: 9 },
    { title: "Landmarks of Spain", text: "The app's main parts are Spain's landmarks. The trip is planned in the Palacio de Cibeles and crosses the Canal to Atocha.", at: "app", dist: 175, polar: 0.98, az: 0.55, secs: 11 },
    { title: "The Canal is the network", text: "Every taxi, bus and courier crossing it is an HTTP request.", target: [-18, 0, -28], dist: 170, polar: 1.02, az: 0.2, secs: 9 },
    { title: "Server Downtown", text: "One Python process plans every trip, along the Paseo de la Castellana.", at: "engine", dist: 210, polar: 1.0, az: 0.9, secs: 10 },
    { title: "Andén 0", text: "Under the streets, the route search runs in Rust.", at: "search_rs", dist: 150, polar: 0.85, az: 0.3, under: true, secs: 9 },
    { title: "Flights cost money", text: "Only Google's Routes API is paid for. The control tower caps it by the day.", at: "atc", dist: 190, polar: 1.02, az: -0.6, secs: 9 },
    { title: "Out past the Canal", text: "Services beyond the computer stand on islands: the Guggenheim, the Torre del Oro, the Aqueduct of Segovia, the Sagrada Família.", target: [186, 0, 10], dist: 300, polar: 1.0, az: -1.05, secs: 11 },
  ];
  function startReel() {
    if (reel || reduced) return;
    clear();
    reel = { i: -1, t: 0, lap: 0, was: night };
    controls.autoRotate = true; controls.autoRotateSpeed = 0.45;
    emit("reel", true);
    reelShot(0);
  }
  function stopReel() {
    if (!reel) return;
    const was = reel.was;
    reel = null; controls.autoRotate = false; caption.hidden = true; reveal(false);
    if (night !== was) { night = was; applyTheme(); }
    emit("reel", false);
  }
  function reelShot(i) {
    if (i >= REEL.length) { i = 0; reel.lap++; night = !isDark(); applyTheme(); } // the next lap in the other light
    const s = REEL[i];
    reel.i = i; reel.t = 0;
    reveal(!!s.under);
    let target, dist = s.dist || 160;
    if (s.whole) { target = new THREE.Vector3(20, 0, -22); dist = overviewDist() * 0.92; }
    else if (s.sample) { target = new THREE.Box3().setFromPoints(SAMPLE.points()).getCenter(new THREE.Vector3()); dist = 230; }
    else if (s.at) target = new THREE.Vector3(byId[s.at].x, byId[s.at].mesh.position.y, byId[s.at].z);
    else target = new THREE.Vector3(s.target[0], s.target[1], s.target[2]);
    flyTo(view(target, dist, s.polar || (s.whole ? 0.84 : 0.9), s.az === undefined ? (s.whole ? 0.08 : 0.5) : s.az), 2.6);
    caption.firstChild.textContent = s.title; caption.lastChild.textContent = s.text;
    caption.hidden = false; caption.classList.remove("in"); void caption.offsetWidth; caption.classList.add("in");
  }
  function updateReel(dt) {
    reel.t += dt;
    if (reel.t > REEL[reel.i].secs) reelShot(reel.i + 1);
  }
  // Whoever touches the page takes over; left alone for IDLE_S, it starts showing itself again.
  function bindIdle() {
    caption = wrap.querySelector(".caption");
    const touched = (e) => {
      idle = 0;
      if (!reel) return;
      // The showreel's own button stops it itself, with a click or with Enter or Space: anything else stops it here.
      const itsButton = e.target.closest && e.target.closest("#show");
      if (itsButton && (e.type !== "keydown" || e.key === "Enter" || e.key === " ")) return;
      stopReel();
    };
    for (const name of ["pointerdown", "pointermove", "wheel", "keydown", "touchstart"]) addEventListener(name, touched, { passive: true, capture: true });
  }
  const toggleReel = () => (reel ? stopReel() : startReel());
  function setNight(v) { if (reel) reel.was = v; night = v; applyTheme(); emit("night", isDark()); }
  const pointsOf = (places) => places.map((p) => new THREE.Vector3(p.x, p.mesh.position.y, p.z));
  function frameBox(points, minDist) {
    const box = new THREE.Box3().setFromPoints(points), c = box.getCenter(new THREE.Vector3()), s = box.getSize(new THREE.Vector3());
    const span = Math.max(s.x, s.z, s.y * 1.3);
    const dist = Math.max(minDist, (span * 1.25) / (2 * Math.tan((15 * Math.PI) / 180) * Math.min(1.1, camera.aspect)));
    c.y = Math.max(c.y, c.y * 0.4);
    flyTo(view(c, Math.min(dist, 760), 0.92));
  }

  // ---- Journeys
  function startTour(id) {
    stopReel(); clear(); SAMPLE.show(false);
    const def = TOURS.find((t) => t.id === id);
    setMap(def.map);
    tour = { def, i: -1, trail: new Set(), seen: new Set(), now: new Set(), playing: !reduced, t: 0, dur: 1, wait: 0, path: null };
    emit("tour", tour);
    enterStep(0);
  }
  function stopTour() {
    tour = null;
    if (tourCar) tourCar.visible = false;
    payload.hidden = true;
    emit("tour", null);
  }
  function enterStep(i) {
    if (!tour) return;
    const s = tour.def.steps[i];
    tour.i = i; tour.t = 0; tour.waited = 0; tour.done = false; tour.path = null;
    ring.visible = false;
    if (s.flow) {
      const f = TRAFFIC.flows[s.flow], m = MODES[f.mode];
      tour.trail.add(f.id); tour.seen.add(f.from); tour.seen.add(f.to);
      tour.now = new Set([f.from, f.to]);
      tour.path = s.dir === "back" ? f.back : f.out;
      tour.dur = Math.max(2, Math.min(6, tour.path.len / (m.speed * 1.5)));
      TRAFFIC.setFocus({ flows: tour.trail, vehicles: false, hideRest: false });
      ghostAll(tour.seen);
      reveal(f.mode === "subway");
      if (tourMode !== f.mode) {
        if (tourCar) scene.remove(tourCar);
        tourCar = TRAFFIC.vehicleFor(f.mode, ["subway", "freight", "ship", "plane"].includes(f.mode) ? 1 : 1.5);
        tourCar.traverse((o) => { if (o.isMesh) o.material = firstVehicleMat(); });
        tourMode = f.mode; scene.add(tourCar);
      }
      tourCar.visible = true;
      payload.hidden = false;
      payload.textContent = s.dir === "back" ? f.reply || "the reply" : f.carries;
      TRAFFIC.pose(tourCar, tour.path, 0);
      frameBox(tour.path.pts, f.mode === "foot" ? 80 : f.mode === "subway" ? 130 : 105);
    } else {
      const p = byId[s.at];
      tour.seen.add(s.at); tour.now = new Set([s.at]);
      TRAFFIC.setFocus({ flows: tour.trail, vehicles: false, hideRest: false });
      ghostAll(tour.seen); reveal(false);
      if (tourCar) tourCar.visible = false;
      payload.hidden = true;
      ring.visible = true; ring.position.set(p.x, p.mesh.position.y + 0.4, p.z); ring.scale.setScalar(14);
      tour.dur = 1;
      frameBox(pointsOf([p]), 70);
    }
    if (reduced && tour.path) { TRAFFIC.pose(tourCar, tour.path, tour.path.len); tour.t = tour.dur; }
    emit("step", tour);
  }
  function nextStep() { if (!tour) return; if (tour.i + 1 < tour.def.steps.length) enterStep(tour.i + 1); else finishTour(); }
  function prevStep() { if (tour && tour.i > 0) enterStep(tour.i - 1); }
  function goStep(i) { if (tour) { tour.playing = false; enterStep(i); emit("step", tour); } }
  function togglePlay() {
    if (!tour) return;
    tour.playing = !tour.playing;
    if (tour.playing && tour.finished) { tour.finished = false; tour.trail.clear(); tour.seen.clear(); enterStep(0); }
    emit("step", tour);
  }
  function finishTour() {
    tour.playing = false; tour.finished = true; tour.now = new Set();
    if (tourCar) tourCar.visible = false;
    payload.hidden = true; ring.visible = false;
    reveal(false);
    frameBox([...tour.seen].flatMap((id) => pointsOf([byId[id]])), 120);
    emit("step", tour);
  }
  function updateTour(dt) {
    if (!tour || tour.finished) return;
    tour.t += dt;
    const k = Math.min(1, tour.t / tour.dur), ease = k * k * (3 - 2 * k);
    if (tour.path && tourCar) {
      TRAFFIC.pose(tourCar, tour.path, tour.path.len * ease);
      V.copy(tourCar.position); V.y += 6; V.project(camera);
      const w = wrap.clientWidth, h = wrap.clientHeight;
      payload.style.transform = `translate(calc(${((V.x + 1) / 2) * w}px - 50%), calc(${((1 - V.y) / 2) * h}px - 100%))`;
      if (tourCar.userData.rotor) tourCar.userData.rotor.rotation.y += dt * 22;
    }
    if (k >= 1 && tour.playing) {
      tour.waited += dt;
      const s = tour.def.steps[tour.i], read = Math.max(2.2, s.body.split(" ").length / 3.4);
      if (tour.waited > read) nextStep();
    }
  }

  // The point the camera turns round stays over the city, and the camera above the street.
  const BOUNDS = [CITY.SLAB[0] - 30, CITY.SLAB[1] + 30, CITY.SLAB[2] - 60, CITY.SLAB[3] + 30];
  function keepInCity() {
    const t = controls.target;
    const x = Math.min(BOUNDS[1], Math.max(BOUNDS[0], t.x)), z = Math.min(BOUNDS[3], Math.max(BOUNDS[2], t.z)), y = Math.min(80, Math.max(-12, t.y));
    if (x !== t.x || y !== t.y || z !== t.z) { camera.position.x += x - t.x; camera.position.y += y - t.y; camera.position.z += z - t.z; t.set(x, y, z); }
    if (camera.position.y < 0.9 && !reel) camera.position.y = 0.9;
  }

  // ---- The loop
  function loop(now) {
    requestAnimationFrame(loop);
    const took = (now - last) / 1000 || 0;
    const dt = Math.min(0.05, took);
    // Once the page has settled, fewer pixels from here on: a step down under about 24 frames a second over
    // three seconds; straight down under 6, or when two frames running take half a second or more each.
    if (!fixedRes && sharpness > LEAST && !resumed && now > 3000 && !document.hidden) {
      crawl = took > 0.5 ? crawl + 1 : 0;
      timed++; span += Math.min(took, 1); // one long hitch counts for no more than a second
      if (crawl >= 2 || span >= 3) {
        const rate = crawl >= 2 ? 0 : timed / span;
        if (rate < 24) { sharpness = rate < 6 ? (sharpness > 1 ? 1 : LEAST) : Math.max(1, sharpness - 0.5); resize(); }
        timed = span = crawl = 0;
      }
    }
    resumed = false;
    last = now; clock += dt; frame++;
    idle += dt;
    if (reel) updateReel(dt);
    else if (attract && idle > IDLE_S && !tour && !document.hidden) startReel();
    if (traffic) {
      SAMPLE.update(dt, clock);
      TRAFFIC.update(dt, clock);
      const w = WORLD.wave(); if (w) w.offset.set(clock * 0.006, clock * 0.004);
    }
    updateTour(dt);
    if (flight) {
      flight.t += dt;
      const k = Math.min(1, flight.t / flight.seconds), e = k < 0.5 ? 4 * k * k * k : 1 - (-2 * k + 2) ** 3 / 2;
      camera.position.lerpVectors(flight.fromPos, flight.pos, e);
      controls.target.lerpVectors(flight.fromTarget, flight.target, e);
      if (k >= 1) flight = null;
    }
    if (zoomGoal && zoomAt) { // eased over a few frames, so a wheel's clicks come out as one smooth move
      const step = reduced || Math.abs(zoomGoal) < 0.003 ? zoomGoal : zoomGoal * Math.min(1, dt * 14);
      zoomBy(Math.exp(step), zoomAt); zoomGoal -= step;
    }
    const orbit = camera.position.distanceTo(controls.target);
    controls.maxPolarAngle = 1.42 + 0.12 * Math.min(1, Math.max(0, (90 - orbit) / 70)); // close up, nearly level, as from the street
    controls.update();
    keepInCity();
    // How near the camera can see: near enough for a balcony up close, far enough out for the depth to stay exact.
    const near = Math.min(5, Math.max(0.08, orbit * 0.03));
    if (Math.abs(near - camera.near) > camera.near * 0.1) { camera.near = near; camera.updateProjectionMatrix(); }
    if (ring.visible && !reduced) ring.material.opacity = 0.6 + 0.35 * Math.sin(clock * 3.2);
    if (pointerSeen && frame % 4 === 0 && !downAt) {
      const hit = pick();
      setHover(hit && hit.place, hit && hit.flow);
    }
    if (frame % 3 === 0) layoutLabels();
    renderer.render(scene, camera);
    renderer.shadowMap.needsUpdate = false;
  }

  const setTraffic = (v) => { traffic = v; emit("traffic", v); };
  // What the last frame took to draw, and how sharply: ENGINE.stats() in the browser's console, to see how a computer copes.
  // distance: from the camera to the point it turns round, which zooming changes. software: drawn without a graphics card.
  const stats = () => ({ triangles: renderer.info.render.triangles, calls: renderer.info.render.calls, pixelRatio: renderer.getPixelRatio(), width: renderer.domElement.width, height: renderer.domElement.height, distance: Math.round(camera.position.distanceTo(controls.target) * 10) / 10, software });
  // The camera put straight at a spot on the ground: from how far, how steeply (0 is from overhead) and from which side.
  const lookAt = (x, z, dist = 120, polar = 0.9, azimuth = 0.4) => flyTo(view(new THREE.Vector3(x, 0, z), dist, polar, azimuth), 0);
  return { init, on, select, selectMode, selectFlow, clear, setMap, startTour, stopTour, nextStep, prevStep, goStep, togglePlay, overview, setTraffic, traffic: () => traffic, byId, reduced, getTour: () => tour, showSample, toggleReel, stopReel, setNight, isDark, lookAt, stats, zoom };
})();
