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
    const [boroughs, nta, subway, zips] = await Promise.all(
      ["boroughs", "nta", "subway", "zips"].map((n) => fetch(url(`static/geo/${n}.json?v=2`)).then((r) => r.json())));
    geo = { boroughs, nta, subway, zips };
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
      paint: { "line-color": ["get", "color"], "line-width": 2, "line-opacity": 0.45 } });
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
    if (on && !this._routesLit) {
      this.animate("busy", (ts) => this.map.setPaintProperty("subway", "line-opacity", 0.35 + 0.3 * Math.abs(Math.sin(ts / 400))));
    } else if (!on) {
      this.stopAnim("busy");
      if (!this._routesLit) this.map.setPaintProperty("subway", "line-opacity", 0.45);
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
    for (const k of [...this.anims.keys()]) if (k !== "busy") this.anims.delete(k);
    this.legendEl.replaceChildren();
    this.chipbar.replaceChildren();
    this.el.parentElement.querySelector(".wxstrip")?.remove();
    if (this._routesLit) {
      this.map.setPaintProperty("subway", "line-opacity", 0.45);
      this.map.setPaintProperty("subway", "line-width", 2);
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
        if (pts.length <= 60) {
          this.animate(id, (ts) => {
            const k = (ts % 1800) / 1800;
            this.map.setPaintProperty(id + "-ring", "circle-stroke-opacity", 0.8 * (1 - k));
            this.map.setPaintProperty(id + "-ring", "circle-radius", vals.length
              ? ["interpolate", ["linear"], ["coalesce", ["get", "value"], 0], 0, 5 + 14 * k, hi || 1, 14 + 16 * k] : 7 + 16 * k);
          });
        }
        pts.forEach((p) => bounds.extend([+p.lon, +p.lat]));
        any = true;
        this.hover(id, (p) => `<b>${esc(p.label)}</b>${p.value != null ? "<br>" + fmt(p.value) + " " + esc(L.unit || "") : ""}${p.detail ? "<br>" + esc(p.detail) : ""}${p.updated ? "<br><small>updated " + esc(p.updated) + "</small>" : ""}`);
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
      this.map.fitBounds(bounds, { padding: 60, maxZoom: 14, duration: 1400 });
    }
  }

  // The places an answer is about: outlined, glowing, numbered and labelled; the map frames them.
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
    items.forEach((it, i) => {
      const b = new maplibregl.LngLatBounds();
      let at = null;
      if (it.level === "point" && Number.isFinite(+it.lat)) {
        at = [+it.lon, +it.lat];
        b.extend(at);
      } else {
        const keys = [].concat(it.key || []).map((k) => String(k).toLowerCase());
        const src = it.level === "zip" ? g.zips : it.level === "borough" ? g.boroughs : g.nta;
        const fs = src.features.filter((f) => keys.includes(String(f.properties.name).toLowerCase()));
        if (!fs.length) return;
        fs.forEach((f) => { feats.push({ ...f, properties: { ...f.properties, i } }); extend(b, f.geometry); });
        at = centroid(biggest(fs).geometry);
      }
      all.extend(b);
      const node = NY.el("div", { class: "hl-mark" + (it.level === "point" ? " pin" : ""), style: `--d:${i * 140}ms` },
        NY.el("span", { class: "num" }, String(i + 1)),
        NY.el("span", { class: "txt" }, NY.el("b", {}, it.name), it.note ? NY.el("small", {}, it.note) : ""));
      node.addEventListener("click", () => this.focus(i));
      const marker = this.marker(at, node, it.level === "point" ? "bottom-left" : "center");
      this.hl.push({ bounds: b, marker, node, point: it.level === "point" ? at : null });
    });
    if (!this.hl.length) return false;
    if (feats.length) {
      this.map.addSource("hl", { type: "geojson", data: { type: "FeatureCollection", features: feats } });
      this.layers.push("hl-fill", "hl-glow", "hl-line", "hl");
      // Over shaded areas the outline and glow are enough; a tint would muddy the shading.
      const shaded = this.layers.some((x) => this.map.getLayer(x)?.type === "fill");
      this.map.addLayer({ id: "hl-fill", type: "fill", source: "hl", paint: { "fill-color": "#3987e5", "fill-opacity": shaded ? 0 : 0.16 } });
      this.map.addLayer({ id: "hl-glow", type: "line", source: "hl", paint: {
        "line-color": "#3987e5", "line-width": 9, "line-blur": 6, "line-opacity": 0.6 } });
      this.map.addLayer({ id: "hl-line", type: "line", source: "hl", paint: { "line-color": this.ink, "line-width": 2.2 } });
      this.animate("hl", (ts) => {
        const k = 0.5 + 0.5 * Math.sin(ts / 520);
        this.map.setPaintProperty("hl-glow", "line-opacity", 0.25 + 0.55 * k);
        this.map.setPaintProperty("hl-glow", "line-width", 6 + 8 * k);
      });
    }
    this.map.fitBounds(all, { padding: { top: 70, bottom: 70, left: 60, right: 60 }, maxZoom: 14, duration: 1500 });
    if (!this._declutter) {
      this._declutter = () => this.declutter();
      this.map.on("moveend", this._declutter);
    }
    this.declutter();
    return true;
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
    if (h.point) this.map.flyTo({ center: h.point, zoom: 15, speed: 0.9 });
    else this.map.fitBounds(h.bounds, { padding: 80, maxZoom: 15, duration: 1100 });
    this.hl.forEach((x) => x.node.classList.toggle("dim", x !== h));
    h.node.classList.remove("ping");
    void h.node.offsetWidth;
    h.node.classList.add("ping");
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
