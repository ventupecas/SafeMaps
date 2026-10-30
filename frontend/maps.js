// The map behind the page: Google's (with live traffic and Google place search)
// or OpenStreetMap's (through Leaflet, with OpenStreetMap place search). The
// server's ROUTES_MAP picks one (GET /app-config). Both offer the same small
// interface, so app.js never touches a map library directly:
//
//   await view.load()                     load the library
//   await view.create(element, options)   { center, zoom, onClick(point), onDragStart() }
//   view.line(points, style)              { color, weight, opacity, dashed } -> { front(on), remove() }
//   view.marker(point, element, options)  { title, zIndex } -> { move(point), remove() }
//   view.fit(points), view.panTo(point), view.setZoom(zoom)
//   view.traffic(on), view.hasTraffic     a traffic layer, when the map has one
//   await view.search(container, placeholder, onPick(place))   place search in a box
//
// Points are { lat, lng }. Google's terms don't allow its results on another
// map, so the OpenStreetMap page never loads Google at all. Its traffic layer
// is TomTom's, served through our server (trafficTiles), which holds the key.

const LEAFLET = "https://unpkg.com/leaflet@1.9.4/dist/";
const OSM_TILES = "https://tile.openstreetmap.org/{z}/{x}/{y}.png";
const OSM_CREDIT = '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors';
const TOMTOM_CREDIT = 'Traffic &copy; <a href="https://www.tomtom.com">TomTom</a>';
const PHOTON = "https://photon.komoot.io/api/";
const SEARCH_BIAS = { lat: 43.0389, lng: -87.9065 }; // prefer places around Milwaukee

// trafficTiles: the OpenStreetMap map's traffic layer, a tile URL template ({z}/{x}/{y}); none means no layer.
export function createMapView(kind, { mapsApiKey, mapId, onAuthFailure, trafficTiles = null } = {}) {
  return kind === "osm" ? new OsmView(trafficTiles) : new GoogleView(mapsApiKey, mapId, onAuthFailure);
}

// ---- Google

class GoogleView {
  hasTraffic = true;

  constructor(apiKey, mapId, onAuthFailure) {
    this.apiKey = apiKey;
    this.mapId = mapId || "DEMO_MAP_ID";
    this.onAuthFailure = onAuthFailure;
  }

  load() {
    return new Promise((resolve, reject) => {
      window.initMap = resolve;
      // Google calls this when it rejects the key (wrong website, service not enabled, ...).
      window.gm_authFailure = () => this.onAuthFailure && this.onAuthFailure();
      const params = new URLSearchParams({
        key: this.apiKey,
        v: "weekly",
        loading: "async",
        libraries: "places,marker",
        callback: "initMap",
      });
      const script = document.createElement("script");
      script.src = `https://maps.googleapis.com/maps/api/js?${params}`;
      script.async = true;
      script.onerror = () => reject(new Error("The map couldn't load. Check the key and your network."));
      document.head.append(script);
    });
  }

  async create(element, { center, zoom, onClick, onDragStart }) {
    this.map = new google.maps.Map(element, {
      center,
      zoom,
      mapId: this.mapId,
      mapTypeControl: false,
      streetViewControl: false,
      fullscreenControl: false,
    });
    this.trafficLayer = new google.maps.TrafficLayer();
    ({ AdvancedMarkerElement: this.Marker } = await google.maps.importLibrary("marker"));
    this.map.addListener("click", (event) => onClick({ lat: event.latLng.lat(), lng: event.latLng.lng() }));
    this.map.addListener("dragstart", onDragStart);
  }

  line(points, { color, weight = 5, opacity = 1, dashed = false, zIndex = 0 }) {
    const options = dashed
      ? {
          strokeOpacity: 0,
          icons: [{ icon: { path: "M 0,-1 0,1", strokeOpacity: 1, strokeColor: color, scale: 3 }, offset: "0", repeat: "14px" }],
        }
      : { strokeColor: color, strokeWeight: weight, strokeOpacity: opacity };
    const line = new google.maps.Polyline({ map: this.map, path: points, clickable: false, zIndex, ...options });
    return {
      front: (on) => line.setOptions({ zIndex: on ? zIndex + 100 : zIndex }),
      remove: () => line.setMap(null),
    };
  }

  marker(point, element, { title = "", zIndex } = {}) {
    const marker = new this.Marker({ map: this.map, position: point, content: element, title, zIndex });
    return {
      move: (to) => {
        marker.position = to;
      },
      remove: () => {
        marker.map = null;
      },
    };
  }

  fit(points) {
    const bounds = new google.maps.LatLngBounds();
    points.forEach((point) => bounds.extend(point));
    if (!bounds.isEmpty()) this.map.fitBounds(bounds, 48);
  }

  panTo(point) {
    this.map.panTo(point);
  }

  setZoom(zoom) {
    this.map.setZoom(zoom);
  }

  traffic(on) {
    this.trafficLayer.setMap(on ? this.map : null);
  }

  async search(container, placeholder, onPick) {
    try {
      const { PlaceAutocompleteElement } = await google.maps.importLibrary("places");
      let search;
      try {
        search = new PlaceAutocompleteElement({
          includedRegionCodes: ["us"],
          locationBias: { center: SEARCH_BIAS, radius: 50000 },
        });
      } catch (error) {
        console.warn("Place search options not accepted; using defaults:", error);
        search = new PlaceAutocompleteElement({});
      }
      search.setAttribute("placeholder", placeholder);
      const pick = async (place) => {
        await place.fetchFields({ fields: ["location", "displayName", "formattedAddress", "addressComponents"] });
        const country = (place.addressComponents || []).find((part) => part.types.includes("country"));
        onPick({
          lat: place.location.lat(),
          lng: place.location.lng(),
          label: place.displayName || place.formattedAddress,
          countryCode: country ? country.shortText : null,
        });
      };
      // Current versions fire "gmp-select"; older ones fired "gmp-placeselect".
      search.addEventListener("gmp-select", (event) => pick(event.placePrediction.toPlace()));
      search.addEventListener("gmp-placeselect", (event) => pick(event.place));
      container.replaceChildren(search);
    } catch (error) {
      console.warn("Place search unavailable:", error);
      container.textContent = "Place search is unavailable. Click the map instead.";
    }
  }
}

// ---- OpenStreetMap (Leaflet)

class OsmView {
  constructor(trafficTiles) {
    this.trafficTiles = trafficTiles;
    this.hasTraffic = Boolean(trafficTiles);
    this.trafficLayer = null;
  }

  async load() {
    const css = document.createElement("link");
    css.rel = "stylesheet";
    css.href = `${LEAFLET}leaflet.css`;
    document.head.append(css);
    await new Promise((resolve, reject) => {
      const script = document.createElement("script");
      script.src = `${LEAFLET}leaflet.js`;
      script.onload = resolve;
      script.onerror = () => reject(new Error("The map couldn't load. Check your network."));
      document.head.append(script);
    });
  }

  async create(element, { center, zoom, onClick, onDragStart }) {
    this.map = L.map(element, { zoomControl: true }).setView([center.lat, center.lng], zoom);
    L.tileLayer(OSM_TILES, { maxZoom: 19, attribution: OSM_CREDIT }).addTo(this.map);
    this.map.on("click", (event) => onClick({ lat: event.latlng.lat, lng: event.latlng.lng }));
    this.map.on("dragstart", onDragStart);
  }

  line(points, { color, weight = 5, opacity = 1, dashed = false }) {
    const line = L.polyline(
      points.map((p) => [p.lat, p.lng]),
      { color, weight: dashed ? 4 : weight, opacity, dashArray: dashed ? "2 10" : null, interactive: false },
    ).addTo(this.map);
    // Leaflet draws in the order lines are added; bring one forward to raise it.
    return { front: (on) => on && line.bringToFront(), remove: () => line.remove() };
  }

  marker(point, element, { title = "", zIndex = 0 } = {}) {
    // Anchored at the bottom middle, like Google's markers, so the same styles fit both.
    const anchor = document.createElement("div");
    anchor.className = "lf-anchor";
    anchor.append(element);
    const marker = L.marker([point.lat, point.lng], {
      icon: L.divIcon({ html: anchor, className: "lf-icon", iconSize: [0, 0] }),
      title,
      zIndexOffset: zIndex * 100,
      interactive: Boolean(title),
      keyboard: false,
    }).addTo(this.map);
    return { move: (to) => marker.setLatLng([to.lat, to.lng]), remove: () => marker.remove() };
  }

  fit(points) {
    if (points.length) this.map.fitBounds(L.latLngBounds(points.map((p) => [p.lat, p.lng])), { padding: [48, 48] });
  }

  panTo(point) {
    this.map.panTo([point.lat, point.lng]);
  }

  setZoom(zoom) {
    this.map.setZoom(zoom);
  }

  // TomTom's traffic map over the streets: green, orange and red by speed. Routes stay on top.
  traffic(on) {
    if (!this.trafficTiles) return;
    this.trafficLayer ||= L.tileLayer(this.trafficTiles, { maxZoom: 19, opacity: 0.85, attribution: TOMTOM_CREDIT });
    if (on) this.trafficLayer.addTo(this.map);
    else this.trafficLayer.remove();
  }

  // Place search through Photon, built on OpenStreetMap data (free, fair use).
  async search(container, placeholder, onPick) {
    const input = document.createElement("input");
    input.type = "search";
    input.className = "osm-search";
    input.placeholder = placeholder;
    input.autocomplete = "off";
    const list = document.createElement("ul");
    list.className = "osm-suggestions";
    list.hidden = true;
    let timer = null;
    let results = [];

    const choose = (place) => {
      input.value = place.label;
      list.hidden = true;
      onPick(place);
    };
    const show = () => {
      list.replaceChildren(
        ...results.map((place) => {
          const item = document.createElement("li");
          item.textContent = place.label;
          item.addEventListener("mousedown", (event) => {
            event.preventDefault(); // keep focus, so blur doesn't hide the list first
            choose(place);
          });
          return item;
        }),
      );
      list.hidden = results.length === 0;
    };
    input.addEventListener("input", () => {
      clearTimeout(timer);
      const text = input.value.trim();
      if (text.length < 3) {
        results = [];
        return show();
      }
      timer = setTimeout(async () => {
        try {
          results = await photon(text);
        } catch (error) {
          console.warn("Place search failed:", error);
          results = [];
        }
        show();
      }, 300);
    });
    input.addEventListener("keydown", (event) => {
      if (event.key === "Enter" && results.length) choose(results[0]);
    });
    input.addEventListener("blur", () => setTimeout(() => (list.hidden = true), 150));
    container.replaceChildren(input, list);
  }
}

async function photon(text) {
  const params = new URLSearchParams({ q: text, lat: SEARCH_BIAS.lat, lon: SEARCH_BIAS.lng, limit: 5, lang: "en" });
  const response = await fetch(`${PHOTON}?${params}`);
  if (!response.ok) throw new Error(`Photon answered ${response.status}`);
  const { features } = await response.json();
  return features.map(({ geometry, properties: p }) => {
    const street = [p.housenumber, p.street].filter(Boolean).join(" ");
    const parts = [p.name, street, p.city || p.district, p.state].filter(Boolean);
    return {
      lat: geometry.coordinates[1],
      lng: geometry.coordinates[0],
      label: [...new Set(parts)].join(", "),
      countryCode: p.countrycode ? p.countrycode.toUpperCase() : null,
    };
  });
}

// ---- Geometry without a map library

// Google's encoded polyline format, which every route from the server uses.
export function decodePolyline(encoded) {
  const points = [];
  let index = 0;
  let lat = 0;
  let lng = 0;
  while (index < encoded.length) {
    for (const axis of [0, 1]) {
      let result = 0;
      let shift = 0;
      let byte;
      do {
        byte = encoded.charCodeAt(index++) - 63;
        result |= (byte & 0x1f) << shift;
        shift += 5;
      } while (byte >= 0x20);
      const delta = result & 1 ? ~(result >> 1) : result >> 1;
      if (axis === 0) lat += delta;
      else lng += delta;
    }
    points.push({ lat: lat / 1e5, lng: lng / 1e5 });
  }
  return points;
}

const EARTH_RADIUS_M = 6371008.8;
const radians = (degrees) => (degrees * Math.PI) / 180;

export function distanceM(a, b) {
  const h =
    Math.sin(radians(b.lat - a.lat) / 2) ** 2 +
    Math.cos(radians(a.lat)) * Math.cos(radians(b.lat)) * Math.sin(radians(b.lng - a.lng) / 2) ** 2;
  return 2 * EARTH_RADIUS_M * Math.asin(Math.min(1, Math.sqrt(h)));
}

// Compass bearing from one point to another, degrees clockwise from north.
export function bearing(from, to) {
  const y = Math.sin(radians(to.lng - from.lng)) * Math.cos(radians(to.lat));
  const x =
    Math.cos(radians(from.lat)) * Math.sin(radians(to.lat)) -
    Math.sin(radians(from.lat)) * Math.cos(radians(to.lat)) * Math.cos(radians(to.lng - from.lng));
  return ((Math.atan2(y, x) * 180) / Math.PI + 360) % 360;
}
