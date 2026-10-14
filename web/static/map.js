// The chat's map: a quiet basemap with borough lines and the subway, and whatever the guide found
// drawn on top: shaded areas, points, road segments, lit subway lines, live weather with the rain
// radar, and the places the answer talks about, numbered and labelled.

import * as maplibregl from "./vendor/maplibre-gl.mjs";

const { url } = NY;
const NYC = [[-74.26, 40.49], [-73.69, 40.92]];
const STYLE = {
  light: "https://tiles.openfreemap.org/styles/positron",
  dark: "https://tiles.openfreemap.org/styles/dark",
};
const SCALES = { value: ["#fff1df", "#f6b26b", "#eb6834", "#a8320f"] };
// The subway map stays in the background unless an answer is about the subway.
const SUBWAY_REST = 0.16;
const ROAD_KIND = ["closure", "incident", "roadwork", "event"];
const ROAD_COLOR = { closure: "#d62d20", incident: "#e8590c", roadwork: "#f2a51a", event: "#7c4dff" };
const ROAD_NAME = { closure: "Closed", incident: "Crash or blockage", roadwork: "Roadwork", event: "Street event" };
const SPEED = ["interpolate", ["linear"], ["get", "value"], 5, "#d62d20", 15, "#f08a24", 25, "#f2c12e", 40, "#2fae2f"];
// Where to put a borough's weather badge (inside the borough, clear of the others).
const BORO_AT = {
  Manhattan: [-73.968, 40.79], Brooklyn: [-73.945, 40.645], Queens: [-73.80, 40.72],
  Bronx: [-73.865, 40.85], "Staten Island": [-74.145, 40.585],
};
// NEXRAD composite reflectivity from the Iowa Environmental Mesonet, 50 minutes ago to now.
const RADAR = [50, 45, 40, 35, 30, 25, 20, 15, 10, 5, 0];
const radarTile = (m) => `https://mesonet.agron.iastate.edu/cache/tile.py/1.0.0/nexrad-n0q-900913${m ? `-m${String(m).padStart(2, "0")}m` : ""}/{z}/{x}/{y}.png`;

let geo = null;
async function geodata() {
  if (!geo) {
    const [boroughs, nta, subway, zips, crossings] = await Promise.all(
      ["boroughs", "nta", "subway", "zips", "crossings"].map((n) => fetch(url(`static/geo/${n}.json?v=2`)).then((r) => r.json())));
    geo = { boroughs, nta, subway, zips, crossings };
  }
  return geo;
}

const theme = () => (document.documentElement.dataset.theme ||
  (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light"));
const reduced = () => matchMedia("(prefers-reduced-motion: reduce)").matches;

export class GuideMap {
  constructor(el, legend, chipbar) {
    this.el = el;
    this.legendEl = legend;
    this.chipbar = chipbar;
    this.layers = [];
    this.markers = [];
    this.anims = new Map();
    this.hl = [];
    this.ready = this.init();
  }

  async init() {
    const g = await geodata();
    this.map = new maplibregl.Map({
      container: this.el, style: STYLE[theme()], bounds: NYC, fitBoundsOptions: { padding: 30 },
      attributionControl: { compact: true }, maxBounds: [[-75.2, 40.0], [-72.7, 41.4]],
    });
    this.map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-left");
    await new Promise((res) => this.map.once("load", res));
    this.el.querySelector(".maplibregl-ctrl-attrib")?.classList.remove("maplibregl-compact-show");
    const m = this.map;
    const dark = theme() === "dark";
    this.ink = dark ? "#f4f3ee" : "#0b0b0b";
    m.addSource("boroughs", { type: "geojson", data: g.boroughs });
    m.addSource("subway", { type: "geojson", data: g.subway });
    m.addLayer({ id: "boro-line", type: "line", source: "boroughs",
      paint: { "line-color": dark ? "#c3c2b7" : "#0b0b0b", "line-width": 1.2, "line-opacity": 0.55 } });
    m.addLayer({ id: "subway", type: "line", source: "subway",
      layout: { "line-cap": "round", "line-join": "round" },
      paint: { "line-color": ["get", "color"], "line-width": 1.4, "line-opacity": SUBWAY_REST } });
    this.popup = new maplibregl.Popup({ closeButton: false, closeOnClick: true, maxWidth: "260px" });
    this.tick = this.tick.bind(this);
  }

  // One animation loop for everything that moves; it stops when nothing does.
  animate(key, fn) {
    if (reduced()) { fn(0); return; }
    this.anims.set(key, fn);
    if (!this._raf) this._raf = requestAnimationFrame(this.tick);
  }
  stopAnim(key) { this.anims.delete(key); }
  // Time since an animation began; with reduced motion everything is already at its end.
  elapsed(t0) { return reduced() ? 1e9 : performance.now() - t0; }
  tick(ts) {
    this._raf = null;
    if (!this.anims.size) return;
    if (!this._last || ts - this._last > 45) {
      this._last = ts;
      for (const fn of this.anims.values()) { try { fn(ts); } catch (_) { /* layer gone */ } }
    }
    this._raf = requestAnimationFrame(this.tick);
  }

  busy(on) {
    this.el.parentElement.classList.toggle("busy", on);
    if (!this.map) return;
    if (!on) {
      this.stopAnim("busy");
    }
  }

  clear() {
    for (const id of this.layers) {
      if (this.map.getLayer(id)) this.map.removeLayer(id);
    }
    for (const id of this.layers) {
      if (this.map.getSource(id)) this.map.removeSource(id);
    }
    this.layers = [];
    this.markers.forEach((mk) => mk.remove());
    this.markers = [];
    this.hl = [];
    this._road = null;
    for (const k of [...this.anims.keys()]) if (k !== "busy") this.anims.delete(k);
    this.legendEl.replaceChildren();
    this.chipbar.replaceChildren();
    this.el.parentElement.querySelector(".wxstrip")?.remove();
    if (this._routesLit) {
      this.map.setPaintProperty("subway", "line-opacity", SUBWAY_REST);
      this.map.setPaintProperty("subway", "line-width", 1.4);
      this._routesLit = false;
    }
  }

  chip(text, cls) {
    const c = NY.el("span", cls ? { class: cls } : {}, text);
    this.chipbar.append(c);
    while (this.chipbar.children.length > 4) this.chipbar.firstChild.remove();
    return c;
  }

  addLayer(spec, before) {
    this.map.addLayer(spec, before);
    this.layers.push(spec.id);
  }

  // MapLibre positions a marker with a transform on its element, so anything that animates
  // goes on an inner element.
  marker(lngLat, node, anchor = "center") {
    const outer = NY.el("div", { class: "mk" }, node);
    const mk = new maplibregl.Marker({ element: outer, anchor }).setLngLat(lngLat).addTo(this.map);
    this.markers.push(mk);
    return mk;
  }

  async show(layers) {
    await this.ready;
    const g = await geodata();
    const bounds = new maplibregl.LngLatBounds();
    let any = false, focused = false;
    for (const L of layers || []) {
      const id = `L${Math.random().toString(36).slice(2, 8)}`;
      if (L.type === "highlight") {
        if (this.highlight(L, g)) return;
        continue;
      }
      if (L.type === "weather") {
        this.weather(L);
        any = true;
        Object.values(BORO_AT).forEach((c) => bounds.extend(c));
        continue;
      }
      if (L.type === "areas") {
        const src = L.level === "nta" ? g.nta : L.level === "zip" ? g.zips : g.boroughs;
        const vals = Object.fromEntries(Object.entries(L.values || {}).map(([k, v]) => [k.toLowerCase(), v]));
        const feats = src.features.filter((f) => f.properties.name.toLowerCase() in vals).map((f) => ({
          ...f, properties: { ...f.properties, value: Number(vals[f.properties.name.toLowerCase()]) || 0 } }));
        if (!feats.length) continue;
        const nums = feats.map((f) => f.properties.value);
        const lo = Math.min(...nums), hi = Math.max(...nums);
        const ramp = SCALES.value;
        const flat = hi - lo <= Math.max(Math.abs(hi), 1) * 0.06;
        const stops = flat ? [lo, ramp[1], lo + 1, ramp[1]] : ramp.flatMap((c, i) => [lo + ((hi - lo) * i) / (ramp.length - 1), c]);
        this.map.addSource(id, { type: "geojson", data: { type: "FeatureCollection", features: feats } });
        this.addLayer({ id, type: "fill", source: id, paint: {
          "fill-color": ["interpolate", ["linear"], ["get", "value"], ...stops],
          "fill-opacity": 0, "fill-opacity-transition": { duration: 700 } } }, "boro-line");
        this.addLayer({ id: id + "-edge", type: "line", source: id, paint: {
          "line-color": theme() === "dark" ? "#0b0b0b" : "#ffffff", "line-width": 0.6, "line-opacity": 0.7 } }, "boro-line");
        requestAnimationFrame(() => this.map.setPaintProperty(id, "fill-opacity", flat ? 0.35 : 0.68));
        this.legend(L.label, L.unit, lo, hi, flat ? [ramp[1], ramp[1]] : ramp);
        feats.forEach((f) => extend(bounds, f.geometry));
        any = true;
        if (L.level === "borough" || feats.length <= 6) {
          feats.forEach((f) => this.marker(centroid(f.geometry), NY.el("div", { class: "val-tag" },
            NY.el("b", {}, withUnit(f.properties.value, L.unit)))));
        }
        const label = (p) => (L.level === "zip" && p.area ? `${esc(p.area)} <small>(${esc(p.name)})</small>` : esc(p.name));
        this.hover(id, (p) => `<b>${label(p)}</b><br>${esc(withUnit(p.value, L.unit))}`);
      } else if (L.type === "points") {
        const pts = (L.points || []).filter((p) => Number.isFinite(+p.lat) && Number.isFinite(+p.lon));
        if (!pts.length) continue;
        const vals = pts.map((p) => +p.value).filter(Number.isFinite);
        const hi = vals.length ? Math.max(...vals) : 1;
        this.map.addSource(id, { type: "geojson", data: { type: "FeatureCollection", features: pts.map((p) => ({
          type: "Feature", geometry: { type: "Point", coordinates: [+p.lon, +p.lat] },
          properties: { label: p.label || "", value: Number.isFinite(+p.value) ? +p.value : null,
            detail: p.detail || "", updated: p.updated_at || "" } })) } });
        const radius = vals.length ? ["interpolate", ["linear"], ["coalesce", ["get", "value"], 0], 0, 5, hi || 1, 14] : 7;
        this.addLayer({ id: id + "-ring", type: "circle", source: id, paint: {
          "circle-color": "rgba(0,0,0,0)", "circle-stroke-color": "#eb6834", "circle-stroke-width": 1.5,
          "circle-radius": radius, "circle-stroke-opacity": 0 } });
        this.addLayer({ id, type: "circle", source: id, paint: {
          "circle-color": "#eb6834", "circle-stroke-color": "#ffffff", "circle-stroke-width": 1.5,
          "circle-radius": radius, "circle-opacity": 0.92 } });
        // Dots pop in, ripple three times and then rest.
        const t0 = performance.now();
        this.map.setPaintProperty(id, "circle-radius", 0);
        this.animate(id, () => {
          const t = this.elapsed(t0), k = (t % 1800) / 1800, grow = Math.min(1, t / 420);
          this.map.setPaintProperty(id, "circle-radius", vals.length ? ["*", grow, radius] : 7 * grow);
          this.map.setPaintProperty(id + "-ring", "circle-stroke-opacity", t < 5400 ? 0.8 * (1 - k) : 0);
          this.map.setPaintProperty(id + "-ring", "circle-radius", vals.length
            ? ["interpolate", ["linear"], ["coalesce", ["get", "value"], 0], 0, 5 + 14 * k, hi || 1, 14 + 16 * k] : 7 + 16 * k);
          if (t >= 5400) this.stopAnim(id);
        });
        pts.forEach((p) => bounds.extend([+p.lon, +p.lat]));
        any = true;
        this.hover(id, (p) => `<b>${esc(p.label)}</b>${p.value != null ? "<br>" + fmt(p.value) + " " + esc(L.unit || "") : ""}${p.detail ? "<br>" + esc(p.detail) : ""}${p.updated ? "<br><small>updated " + esc(p.updated) + "</small>" : ""}`);
        if (L.label) this.chip(L.label);
      } else if (L.type === "road_events" && (L.events || []).length) {
        // Closed stretches are drawn along the road itself; the rest are dots, one color per kind.
        // Several calls in one answer (closures, then roadwork) share one layer, closures on top.
        const had = this._road && this.map.getSource(this._road.id) ? this._road : null;
        const seen = new Set((had?.events || []).map(roadKey));
        const events = [...(had?.events || []), ...L.events.filter((e) => e.geometry && !seen.has(roadKey(e)))];
        const feats = events.map((e) => ({ type: "Feature", geometry: e.geometry,
          properties: { kind: e.kind, title: e.title, what: e.what, when: e.when, detail: e.detail,
            rank: ROAD_KIND.indexOf(e.kind) } }))
          .sort((a, b) => b.properties.rank - a.properties.rank);
        if (!feats.length) continue;
        const counts = ROAD_KIND.map((k) => [k, feats.filter((f) => f.properties.kind === k).length]).filter(([, n]) => n);
        const keyRows = counts.map(([k, n]) => [ROAD_COLOR[k], `${ROAD_NAME[k]} (${n})`]);
        feats.forEach((f) => extend(bounds, f.geometry));
        any = true;
        if (had) {
          had.events = events;
          this.map.getSource(had.id).setData({ type: "FeatureCollection", features: feats });
          this.key(L.label || "Road events", keyRows);
          continue;
        }
        this._road = { id, events };
        const color = ["match", ["get", "kind"], ...ROAD_KIND.flatMap((k) => [k, ROAD_COLOR[k]]), ROAD_COLOR.roadwork];
        const isLine = ["==", ["geometry-type"], "LineString"], isPt = ["==", ["geometry-type"], "Point"];
        const closed = ["==", ["get", "kind"], "closure"];
        this.map.addSource(id, { type: "geojson", lineMetrics: true, data: { type: "FeatureCollection", features: feats } });
        this.addLayer({ id: id + "-glow", type: "line", source: id, filter: ["all", isLine, closed],
          layout: { "line-cap": "round", "line-join": "round" },
          paint: { "line-color": ROAD_COLOR.closure, "line-width": 14, "line-blur": 6, "line-opacity": 0.35 } });
        this.addLayer({ id: id + "-case", type: "line", source: id, filter: isLine,
          layout: { "line-cap": "round", "line-join": "round" },
          paint: { "line-color": theme() === "dark" ? "#0b0b0b" : "#ffffff", "line-width": 8 } });
        this.addLayer({ id: id + "-work", type: "line", source: id, filter: ["all", isLine, ["!", closed]],
          layout: { "line-cap": "butt", "line-join": "round" },
          paint: { "line-color": color, "line-width": 3.5, "line-dasharray": [2, 1.4] } });
        this.addLayer({ id: id + "-line", type: "line", source: id, filter: ["all", isLine, closed],
          layout: { "line-cap": "round", "line-join": "round" },
          paint: { "line-width": 5, "line-gradient": ["step", ["line-progress"], ROAD_COLOR.closure, 0, "rgba(0,0,0,0)"] } });
        this.addLayer({ id: id + "-ring", type: "circle", source: id, filter: ["all", isPt, closed],
          paint: { "circle-color": "rgba(0,0,0,0)", "circle-stroke-color": ROAD_COLOR.closure,
            "circle-stroke-width": 1.5, "circle-radius": 7, "circle-stroke-opacity": 0 } });
        this.addLayer({ id: id + "-dot", type: "circle", source: id, filter: isPt,
          layout: { "circle-sort-key": ["-", 9, ["get", "rank"]] },
          paint: { "circle-color": color, "circle-radius": ["case", closed, 7, 5.5],
            "circle-stroke-color": "#ffffff", "circle-stroke-width": 1.5 } });
        // A closed road draws itself along its length, glows for three beats, then holds.
        const t0 = performance.now();
        this.animate(id, () => {
          const t = this.elapsed(t0), k = (t % 1800) / 1800;
          const drawn = Math.min(1, t / 1100), wave = t < 5400 ? Math.sin((t / 1800) * Math.PI) ** 2 : 0;
          this.map.setPaintProperty(id + "-line", "line-gradient", drawn < 1
            ? ["step", ["line-progress"], ROAD_COLOR.closure, drawn, "rgba(0,0,0,0)"]
            : ["step", ["line-progress"], ROAD_COLOR.closure, 1, ROAD_COLOR.closure]);
          this.map.setPaintProperty(id + "-glow", "line-opacity", 0.2 + 0.4 * wave);
          this.map.setPaintProperty(id + "-ring", "circle-stroke-opacity", t < 5400 ? 0.8 * (1 - k) : 0);
          this.map.setPaintProperty(id + "-ring", "circle-radius", 7 + 14 * k);
          if (t >= 5400) this.stopAnim(id);
        });
        const tip = (p) => `<b>${esc(p.title)}</b><br>${esc(p.what)} · <i>${esc(p.when)}</i>${p.detail ? `<br><small>${esc(p.detail)}</small>` : ""}`;
        this.hover(id + "-line", tip);
        this.hover(id + "-work", tip);
        this.hover(id + "-dot", tip);
        this.key(L.label || "Road events", keyRows);
      } else if (L.type === "route" && (L.legs || []).length) {
        // A planned trip: each ride in its line's color and each walk dotted, drawn leg by leg
        // from start to finish, with a dot where the reader boards, transfers and gets off.
        const legs = L.legs.filter((l) => (l.coords || []).length > 1);
        if (!legs.length) continue;
        const ink = theme() === "dark" ? "#0b0b0b" : "#ffffff";
        const stops = [];
        legs.forEach((l, k) => {
          const lid = `${id}-${k}`, walk = l.mode === "walk";
          this.map.addSource(lid, { type: "geojson", lineMetrics: true,
            data: { type: "Feature", geometry: { type: "LineString", coordinates: l.coords }, properties: {} } });
          if (!walk) this.addLayer({ id: lid + "-case", type: "line", source: lid,
            layout: { "line-cap": "round", "line-join": "round" },
            paint: { "line-color": ink, "line-width": 9, "line-opacity": 0 } });
          this.addLayer({ id: lid, type: "line", source: lid, layout: { "line-cap": "round", "line-join": "round" },
            paint: walk
              ? { "line-color": "#8a8f98", "line-width": 3, "line-dasharray": [0.6, 1.6], "line-opacity": 0 }
              : { "line-width": 5.5, "line-gradient": ["step", ["line-progress"], "rgba(0,0,0,0)", 0, "rgba(0,0,0,0)"] } });
          if (!walk) stops.push(l.coords[0], l.coords[l.coords.length - 1]);
          l.coords.forEach((c) => bounds.extend(c));
        });
        if (stops.length) {
          this.map.addSource(id + "-stops", { type: "geojson", data: { type: "FeatureCollection",
            features: stops.map((c) => ({ type: "Feature", geometry: { type: "Point", coordinates: c }, properties: {} })) } });
          this.addLayer({ id: id + "-stops", type: "circle", source: id + "-stops", paint: {
            "circle-color": ink, "circle-stroke-color": this.ink, "circle-stroke-width": 2,
            "circle-radius": 4.5, "circle-opacity": 0, "circle-stroke-opacity": 0 } });
        }
        const t0 = performance.now(), per = 700;
        this.animate(id, () => {
          const t = this.elapsed(t0);
          legs.forEach((l, k) => {
            const lid = `${id}-${k}`, d = Math.max(0, Math.min(1, (t - k * per) / per));
            if (l.mode === "walk") { this.map.setPaintProperty(lid, "line-opacity", d > 0 ? 0.9 : 0); return; }
            const c = l.color || "#3987e5";
            this.map.setPaintProperty(lid, "line-gradient", ["step", ["line-progress"], c, d, "rgba(0,0,0,0)"]);
            this.map.setPaintProperty(lid + "-case", "line-opacity", d > 0 ? 0.9 : 0);
          });
          if (this.map.getLayer(id + "-stops")) {
            const o = Math.min(1, Math.max(0, (t - legs.length * per) / 300));
            this.map.setPaintProperty(id + "-stops", "circle-opacity", o);
            this.map.setPaintProperty(id + "-stops", "circle-stroke-opacity", o);
          }
          if (t >= legs.length * per + 300) this.stopAnim(id);
        });
        any = true;
        if (L.label) this.chip(L.label);
      } else if (L.type === "lines") {
        const lines = (L.lines || []).filter((l) => (l.coords || []).length > 1);
        if (!lines.length) continue;
        this.map.addSource(id, { type: "geojson", data: { type: "FeatureCollection", features: lines.map((l) => ({
          type: "Feature", geometry: { type: "LineString", coordinates: l.coords },
          properties: { label: l.label, value: l.value } })) } });
        this.addLayer({ id, type: "line", source: id, layout: { "line-cap": "round" },
          paint: { "line-color": L.scale === "speed" ? SPEED : "#eb6834", "line-width": 4, "line-opacity": 0.9 } });
        lines.forEach((l) => l.coords.forEach((c) => bounds.extend(c)));
        any = true;
        this.hover(id, (p) => `<b>${esc(p.label)}</b><br>${fmt(p.value)} ${esc(L.unit || "")}`);
        if (L.scale === "speed") this.legend(L.label || "Road speed", "mph", 5, 40, ["#d62d20", "#f08a24", "#f2c12e", "#2fae2f"]);
      } else if (L.type === "routes" && (L.routes || []).length) {
        const routes = L.routes.map(String);
        const hit = ["in", ["get", "route"], ["literal", routes]];
        this.stopAnim("busy");
        this.map.setPaintProperty("subway", "line-opacity", ["case", hit, 0.95, 0.1]);
        this.map.setPaintProperty("subway", "line-width", ["case", hit, 5, 1.5]);
        this._routesLit = true;
        // Moving dashes along the affected lines.
        this.addLayer({ id, type: "line", source: "subway", filter: hit,
          layout: { "line-cap": "butt", "line-join": "round" },
          paint: { "line-color": "#ffffff", "line-width": 2, "line-opacity": 0.85, "line-dasharray": [0, 4, 3] } });
        const seq = [[0, 4, 3], [0.5, 4, 2.5], [1, 4, 2], [1.5, 4, 1.5], [2, 4, 1], [2.5, 4, 0.5], [3, 4, 0],
          [0, 0.5, 3, 3.5], [0, 1, 3, 3], [0, 1.5, 3, 2.5], [0, 2, 3, 2], [0, 2.5, 3, 1.5], [0, 3, 3, 1], [0, 3.5, 3, 0.5]];
        let step = -1;
        this.animate(id, (ts) => {
          const s = Math.floor(ts / 70) % seq.length;
          if (s !== step) { step = s; this.map.setPaintProperty(id, "line-dasharray", seq[s]); }
        });
        g.subway.features.filter((f) => routes.includes(String(f.properties.route)))
          .forEach((f) => extend(bounds, f.geometry));
        any = true;
        this.chip(NY.el("span", { class: "routes" }, (L.label || "Lines") + ": ",
          ...routes.map((r) => NY.el("b", { class: "bullet-mini", style: `background:${routeColor(g, r)}` }, r))));
      } else if (L.type === "focus" && Number.isFinite(+L.lat)) {
        this.map.flyTo({ center: [+L.lon, +L.lat], zoom: L.zoom || 13, speed: 0.9, curve: 1.4 });
        focused = true;
      }
    }
    if (any && !focused && !bounds.isEmpty()) {
      this.map.fitBounds(bounds, { padding: this.pad(60), maxZoom: 14, duration: 1400 });
    }
  }

  // The places an answer is about. They are drawn hidden and framed first, then each one lights
  // up when the answer reaches it: the outline fades in, glows for a few beats and settles.
  highlight(L, g) {
    const items = (L.items || []).slice(0, 6);
    if (!items.length) return false;
    for (const id of ["hl-fill", "hl-glow", "hl-line"]) {
      if (this.map.getLayer(id)) this.map.removeLayer(id);
    }
    if (this.map.getSource("hl")) this.map.removeSource("hl");
    this.hl.forEach((h) => h.marker?.remove());
    this.hl = [];
    const feats = [];
    const all = new maplibregl.LngLatBounds();
    items.forEach((it) => {
      const i = this.hl.length;
      const b = new maplibregl.LngLatBounds();
      let at = null;
      if (it.level === "point" && Number.isFinite(+it.lat)) {
        at = [+it.lon, +it.lat];
        b.extend(at);
      } else {
        const keys = [].concat(it.key || []).map((k) => String(k).toLowerCase());
        const src = { zip: g.zips, borough: g.boroughs, crossing: g.crossings }[it.level] || g.nta;
        const fs = src.features.filter((f) => keys.includes(String(f.properties.name).toLowerCase()));
        if (!fs.length) return;
        fs.forEach((f) => { feats.push({ ...f, properties: { ...f.properties, i } }); extend(b, f.geometry); });
        at = centroid(biggest(fs).geometry);
      }
      all.extend(b);
      const node = NY.el("div", { class: "hl-mark wait" + (it.level === "point" ? " pin" : "") },
        NY.el("span", { class: "num" }, String(i + 1)),
        NY.el("span", { class: "txt" }, NY.el("b", {}, it.name), it.note ? NY.el("small", {}, it.note) : ""));
      node.addEventListener("click", () => this.focus(i));
      const marker = this.marker(at, node, it.level === "point" ? "bottom-left" : "center");
      this.hl.push({ bounds: b, marker, node, point: it.level === "point" ? at : null, lit: 0 });
    });
    if (!this.hl.length) return false;
    if (feats.length) {
      this.map.addSource("hl", { type: "geojson", data: { type: "FeatureCollection", features: feats }, promoteId: "i" });
      this.layers.push("hl-fill", "hl-glow", "hl-line", "hl");
      // Over shaded areas the outline and glow are enough; a tint would muddy the shading.
      const shaded = this.layers.some((x) => this.map.getLayer(x)?.type === "fill");
      const on = ["coalesce", ["feature-state", "on"], 0], beat = ["coalesce", ["feature-state", "beat"], 0];
      this.map.addLayer({ id: "hl-fill", type: "fill", source: "hl",
        paint: { "fill-color": "#3987e5", "fill-opacity": ["*", on, shaded ? 0 : 0.16] } });
      this.map.addLayer({ id: "hl-glow", type: "line", source: "hl", layout: { "line-join": "round", "line-cap": "round" },
        paint: { "line-color": "#3987e5", "line-blur": 6, "line-width": ["+", 5, ["*", 10, beat]],
          "line-opacity": ["*", on, ["+", 0.22, ["*", 0.6, beat]]] } });
      this.map.addLayer({ id: "hl-line", type: "line", source: "hl", layout: { "line-join": "round", "line-cap": "round" },
        paint: { "line-color": this.ink, "line-width": ["case", ["==", ["geometry-type"], "LineString"], 4, 2.2], "line-opacity": on } });
    }
    this._hlFeats = feats.length > 0;
    this.map.fitBounds(all, { padding: this.pad(70), maxZoom: 14, duration: 1400 });
    if (!this._declutter) {
      this._declutter = () => this.declutter();
      this.map.on("moveend", this._declutter);
    }
    this.declutter();
    const early = this._early || [];
    this._early = null;
    if (reduced()) this.revealAll();
    else early.forEach((i) => this.reveal(i));
    return true;
  }

  // Light up place i: its number pops in and its outline glows for three beats, then rests.
  reveal(i) {
    if (!this.hl.length) { (this._early ||= []).push(i); return; }
    const h = this.hl[i];
    if (!h || h.lit) return;
    h.lit = performance.now();
    h.node.classList.remove("wait");
    h.node.classList.add("in");
    this.glow();
  }

  glow() {
    if (!this._hlFeats) return;
    if (reduced()) {
      this.hl.forEach((x, j) => x.lit && this.map.setFeatureState({ source: "hl", id: j }, { on: 1, beat: 0 }));
      return;
    }
    this.animate("hl", () => {
      let busy = false;
      this.hl.forEach((x, j) => {
        if (!x.lit || x.done) return;
        const t = performance.now() - x.lit;
        const on = Math.min(1, t / 450), beat = t < 2700 ? Math.sin((t / 900) * Math.PI) ** 2 : 0;
        this.map.setFeatureState({ source: "hl", id: j }, { on, beat });
        if (t >= 2700) x.done = true; else busy = true;
      });
      if (!busy) this.stopAnim("hl");
    });
  }

  revealAll() { this.hl.forEach((_, i) => this.reveal(i)); }

  // Room around framed places, keeping them clear of the legend in the bottom corner.
  pad(n) {
    const h = this.legendEl.children.length ? this.legendEl.offsetHeight + 24 : 0;
    return { top: n, right: n, left: n, bottom: Math.max(n, Math.min(h, this.el.clientHeight / 3)) };
  }

  // Labels that would sit on top of an earlier one shrink to just their number.
  declutter() {
    const placed = [];
    for (const h of this.hl) {
      const p = this.map.project(h.marker.getLngLat());
      const clash = placed.some((q) => Math.abs(q.x - p.x) < 150 && Math.abs(q.y - p.y) < 38);
      h.node.classList.toggle("short", clash);
      if (!clash) placed.push(p);
    }
  }

  focus(i) {
    const h = this.hl[i];
    if (!h) return;
    this.reveal(i);
    if (h.point) this.map.flyTo({ center: h.point, zoom: 15, speed: 0.9 });
    else this.map.fitBounds(h.bounds, { padding: 80, maxZoom: 15, duration: 1100 });
    this.hl.forEach((x) => x.node.classList.toggle("dim", x !== h));
    h.node.classList.remove("ping");
    void h.node.offsetWidth;
    h.node.classList.add("ping");
    if (h.done) {
      h.done = false;
      h.lit = performance.now() - 450; // glow again without fading the outline back in
      this.glow();
    }
    clearTimeout(this._undim);
    this._undim = setTimeout(() => this.hl.forEach((x) => x.node.classList.remove("dim")), 4000);
  }

  // Weather by borough as animated badges, the radar loop of the last 50 minutes, and the
  // daily forecast along the bottom when one was asked for.
  weather(L) {
    for (const b of L.boroughs || []) {
      const at = BORO_AT[b.name];
      if (!at) continue;
      const node = NY.el("div", { class: "wx" });
      node.innerHTML = wxIcon(b.icon, b.day);
      node.append(NY.el("span", { class: "t" }, `${b.temp_f}°`),
        NY.el("span", { class: "r" + (b.rain_chance_pct >= 40 ? " wet" : "") }, `${b.rain_chance_pct}%`));
      node.title = `${b.name}: ${b.sky}, ${b.temp_f}°F, up to ${b.rain_chance_pct}% chance of rain in the next ${L.hours || 6} h`;
      this.marker(at, node);
    }
    if (L.radar) this.radar(L.raining);
    if ((L.alerts || []).length) this.chip("⚠ " + L.alerts.join(", "), "alert");
  }

  radar(raining) {
    const ids = RADAR.map((m) => `radar-${m}`);
    RADAR.forEach((m, i) => {
      this.map.addSource(ids[i], { type: "raster", tiles: [radarTile(m)], tileSize: 256,
        attribution: "Radar: NWS NEXRAD via Iowa Environmental Mesonet" });
      this.layers.push(ids[i]);
      this.map.addLayer({ id: ids[i], type: "raster", source: ids[i],
        paint: { "raster-opacity": i === RADAR.length - 1 ? 0.7 : 0, "raster-fade-duration": 0 } }, "boro-line");
    });
    const label = this.chip(raining ? "Rain radar · now" : "Rain radar · no rain nearby", "radar");
    if (!raining) return;
    let shown = RADAR.length - 1, last = 0;
    this.animate("radar", (ts) => {
      if (ts - last < (shown === RADAR.length - 1 ? 1600 : 420)) return;
      last = ts;
      const next = (shown + 1) % RADAR.length;
      this.map.setPaintProperty(ids[shown], "raster-opacity", 0);
      this.map.setPaintProperty(ids[next], "raster-opacity", 0.7);
      shown = next;
      label.textContent = `Rain radar · ${RADAR[next] ? `${RADAR[next]} min ago` : "now"}`;
    });
  }

  legend(title, unit, lo, hi, ramp) {
    const block = NY.el("div", { class: "lg" },
      NY.el("b", {}, title || "Value"),
      NY.el("div", { class: "bar", style: `background:linear-gradient(90deg,${ramp.join(",")})` }),
      NY.el("div", { class: "ends" }, NY.el("span", {}, withUnit(lo, unit)), NY.el("span", {}, withUnit(hi, unit))));
    for (const old of this.legendEl.querySelectorAll(".lg")) {
      if (old.firstChild.textContent === (title || "Value")) old.remove();
    }
    this.legendEl.append(block);
    while (this.legendEl.children.length > 2) this.legendEl.firstChild.remove();
  }

  key(title, rows) {
    const block = NY.el("div", { class: "lg" }, NY.el("b", {}, title),
      ...rows.map(([c, text]) => NY.el("div", { class: "key" }, NY.el("i", { style: `background:${c}` }), text)));
    for (const old of this.legendEl.querySelectorAll(".lg")) {
      if (old.firstChild.textContent === title) old.remove();
    }
    this.legendEl.append(block);
    while (this.legendEl.children.length > 2) this.legendEl.firstChild.remove();
  }

  hover(id, html) {
    this.map.on("click", id, (e) => {
      const f = e.features && e.features[0];
      if (f) this.popup.setLngLat(e.lngLat).setHTML(html(f.properties)).addTo(this.map);
    });
    this.map.on("mouseenter", id, () => { this.map.getCanvas().style.cursor = "pointer"; });
    this.map.on("mouseleave", id, () => { this.map.getCanvas().style.cursor = ""; });
  }

  resize() { if (this.map) this.map.resize(); }
}

const ICONS = {
  sun: '<g class="sun"><circle cx="16" cy="16" r="6" fill="#f5b400"/><g class="rays" stroke="#f5b400" stroke-width="2" stroke-linecap="round"><path d="M16 3v3M16 26v3M3 16h3M26 16h3M6.8 6.8l2.1 2.1M23.1 23.1l2.1 2.1M6.8 25.2l2.1-2.1M23.1 8.9l2.1-2.1"/></g></g>',
  moon: '<path d="M20 5a11 11 0 1 0 7 19A9 9 0 0 1 20 5z" fill="#c9c7e8"/>',
  cloud: '<path class="cl" d="M10 25h13a5.5 5.5 0 0 0 .6-11A7.5 7.5 0 0 0 9.4 16 4.5 4.5 0 0 0 10 25z" fill="#b9c2cf" stroke="#8d97a5" stroke-width="1"/>',
  drops: '<g class="drops" stroke="#3987e5" stroke-width="2" stroke-linecap="round"><path d="M12 27l-1 3"/><path d="M17 27l-1 3"/><path d="M22 27l-1 3"/></g>',
  flakes: '<g class="drops" fill="#9cc3ef"><circle cx="12" cy="28.5" r="1.5"/><circle cx="17" cy="29.5" r="1.5"/><circle cx="22" cy="28.5" r="1.5"/></g>',
  bolt: '<path class="bolt" d="M17 22l-3 6h3l-2 4 5-7h-3l2-3z" fill="#f5b400"/>',
  fog: '<g stroke="#9aa3ad" stroke-width="2" stroke-linecap="round"><path class="fog1" d="M7 14h18"/><path class="fog2" d="M5 19h20"/><path class="fog1" d="M8 24h16"/></g>',
};
export function wxIcon(kind, day = true) {
  const sky = day ? ICONS.sun : ICONS.moon;
  const shift = (s) => `<g transform="translate(-5 -5) scale(.8)">${s}</g>`;
  const body = {
    clear: sky,
    partly: shift(sky) + ICONS.cloud,
    cloudy: ICONS.cloud,
    fog: ICONS.fog,
    drizzle: ICONS.cloud + ICONS.drops,
    rain: ICONS.cloud + ICONS.drops,
    snow: ICONS.cloud + ICONS.flakes,
    storm: ICONS.cloud + ICONS.bolt + ICONS.drops,
  }[kind] || ICONS.cloud;
  return `<svg class="wxi ${esc(kind)}" viewBox="0 0 32 34" width="30" height="32" aria-hidden="true">${body}</svg>`;
}

function routeColor(g, r) {
  const f = g.subway.features.find((x) => String(x.properties.route) === r);
  return f ? f.properties.color : "#808183";
}
function roadKey(e) {
  return [e.title, e.what, e.when, e.detail].join("|");
}
function extend(bounds, geom) {
  const walk = (c) => (typeof c[0] === "number" ? bounds.extend(c) : c.forEach(walk));
  walk(geom.coordinates);
}
function rings(geom) {
  return geom.type === "Polygon" ? [geom.coordinates[0]] : geom.type === "MultiPolygon" ? geom.coordinates.map((p) => p[0]) : [];
}
function ringArea(r) {
  let a = 0;
  for (let i = 0, j = r.length - 1; i < r.length; j = i++) a += (r[j][0] + r[i][0]) * (r[j][1] - r[i][1]);
  return Math.abs(a / 2);
}
function biggest(features) {
  return features.reduce((best, f) => {
    const a = Math.max(0, ...rings(f.geometry).map(ringArea));
    return a > best.a ? { f, a } : best;
  }, { f: features[0], a: -1 }).f;
}
// The centre of the largest ring, nudged inside if the polygon is oddly shaped.
function centroid(geom) {
  if (geom.type === "LineString") return geom.coordinates[Math.floor(geom.coordinates.length / 2)];
  if (geom.type === "MultiLineString") {
    const longest = geom.coordinates.reduce((a, b) => (b.length > a.length ? b : a));
    return longest[Math.floor(longest.length / 2)];
  }
  const rs = rings(geom);
  if (!rs.length) return geom.coordinates;
  const r = rs.reduce((a, b) => (ringArea(b) > ringArea(a) ? b : a));
  let x = 0, y = 0, a = 0;
  for (let i = 0, j = r.length - 1; i < r.length; j = i++) {
    const f = r[j][0] * r[i][1] - r[i][0] * r[j][1];
    x += (r[j][0] + r[i][0]) * f; y += (r[j][1] + r[i][1]) * f; a += f;
  }
  if (!a) return r[0];
  const c = [x / (3 * a), y / (3 * a)];
  return inside(c, r) ? c : r[Math.floor(r.length / 2)];
}
function inside([px, py], r) {
  let hit = false;
  for (let i = 0, j = r.length - 1; i < r.length; j = i++) {
    const [xi, yi] = r[i], [xj, yj] = r[j];
    if ((yi > py) !== (yj > py) && px < ((xj - xi) * (py - yi)) / (yj - yi) + xi) hit = !hit;
  }
  return hit;
}
function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}
function withUnit(v, unit) {
  const u = unit || "";
  if (u.startsWith("$")) return `$${fmt(v)}${u.slice(1)}`;
  if (u === "%") return `${fmt(v)}%`;
  return u ? `${fmt(v)} ${u}` : fmt(v);
}
function fmt(v) {
  const n = Number(v);
  if (!Number.isFinite(n)) return esc(v);
  return Math.abs(n) >= 100 ? Math.round(n).toLocaleString("en-US") : n.toLocaleString("en-US", { maximumFractionDigits: 1 });
}
