// The chat's map: a quiet basemap with borough lines and the subway, and whatever the guide's
// tools found drawn on top: shaded boroughs or neighborhoods, points, and road segments.

import * as maplibregl from "./vendor/maplibre-gl.mjs";

const { url } = NY;
const NYC = [[-74.26, 40.49], [-73.69, 40.92]];
const STYLE = {
  light: "https://tiles.openfreemap.org/styles/positron",
  dark: "https://tiles.openfreemap.org/styles/dark",
};
const SCALES = {
  rain: ["#e6f0fb", "#9cc3ef", "#3987e5", "#1b4f99"],
  value: ["#fff1df", "#f6b26b", "#eb6834", "#a8320f"],
};
const SPEED = ["interpolate", ["linear"], ["get", "value"], 5, "#d62d20", 15, "#f08a24", 25, "#f2c12e", 40, "#2fae2f"];

let geo = null;
async function geodata() {
  if (!geo) {
    const [boroughs, nta, subway, zips] = await Promise.all(
      ["boroughs", "nta", "subway", "zips"].map((n) => fetch(url(`static/geo/${n}.json`)).then((r) => r.json())));
    geo = { boroughs, nta, subway, zips };
  }
  return geo;
}

const theme = () => (document.documentElement.dataset.theme ||
  (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light"));

export class GuideMap {
  constructor(el, legend, chipbar) {
    this.el = el;
    this.legendEl = legend;
    this.chipbar = chipbar;
    this.layers = [];
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
    const m = this.map;
    m.addSource("boroughs", { type: "geojson", data: g.boroughs });
    m.addSource("subway", { type: "geojson", data: g.subway });
    m.addLayer({ id: "boro-line", type: "line", source: "boroughs",
      paint: { "line-color": theme() === "dark" ? "#c3c2b7" : "#0b0b0b", "line-width": 1.2, "line-opacity": 0.55 } });
    m.addLayer({ id: "subway", type: "line", source: "subway",
      layout: { "line-cap": "round", "line-join": "round" },
      paint: { "line-color": ["get", "color"], "line-width": 2, "line-opacity": 0.45 } });
    this.popup = new maplibregl.Popup({ closeButton: false, closeOnClick: true, maxWidth: "260px" });
  }

  busy(on) {
    this.el.parentElement.classList.toggle("busy", on);
    if (!this.map) return;
    if (on && !this._pulse) {
      let t = 0;
      this._pulse = setInterval(() => {
        t += 1;
        if (this.map.getLayer("subway") && !this._routesLit) this.map.setPaintProperty("subway", "line-opacity", 0.35 + 0.3 * Math.abs(Math.sin(t / 3)));
      }, 120);
    } else if (!on && this._pulse) {
      clearInterval(this._pulse);
      this._pulse = null;
      if (this.map.getLayer("subway") && !this._routesLit) this.map.setPaintProperty("subway", "line-opacity", 0.45);
    }
  }

  clear() {
    for (const id of this.layers) {
      if (this.map.getLayer(id)) this.map.removeLayer(id);
      if (this.map.getSource(id)) this.map.removeSource(id);
    }
    this.layers = [];
    this.legendEl.replaceChildren();
    this.chipbar.replaceChildren();
    if (this._routesLit) {
      this.map.setPaintProperty("subway", "line-opacity", 0.45);
      this.map.setPaintProperty("subway", "line-width", 2);
      this._routesLit = false;
    }
  }

  chip(text) {
    this.chipbar.append(NY.el("span", {}, text));
    while (this.chipbar.children.length > 4) this.chipbar.firstChild.remove();
  }

  async show(layers) {
    await this.ready;
    const g = await geodata();
    const bounds = new maplibregl.LngLatBounds();
    let any = false;
    for (const L of layers || []) {
      const id = `L${Math.random().toString(36).slice(2, 8)}`;
      if (L.type === "areas") {
        const src = L.level === "nta" ? g.nta : L.level === "zip" ? g.zips : g.boroughs;
        const vals = Object.fromEntries(Object.entries(L.values || {}).map(([k, v]) => [k.toLowerCase(), v]));
        const feats = src.features.filter((f) => f.properties.name.toLowerCase() in vals).map((f) => ({
          ...f, properties: { ...f.properties, value: Number(vals[f.properties.name.toLowerCase()]) || 0 } }));
        if (!feats.length) continue;
        const nums = feats.map((f) => f.properties.value);
        let lo = Math.min(...nums), hi = Math.max(...nums);
        if (L.scale === "rain") { lo = 0; hi = 100; }
        const ramp = SCALES[L.scale === "rain" ? "rain" : "value"];
        const stops = hi > lo ? ramp.flatMap((c, i) => [lo + ((hi - lo) * i) / (ramp.length - 1), c]) : [lo, ramp[2], lo + 1, ramp[2]];
        this.map.addSource(id, { type: "geojson", data: { type: "FeatureCollection", features: feats } });
        this.map.addLayer({ id, type: "fill", source: id, paint: {
          "fill-color": ["interpolate", ["linear"], ["get", "value"], ...stops],
          "fill-opacity": 0 } }, "boro-line");
        this.map.setPaintProperty(id, "fill-opacity-transition", { duration: 700 });
        requestAnimationFrame(() => this.map.setPaintProperty(id, "fill-opacity", 0.62));
        this.layers.push(id);
        this.legend(L.label, L.unit, lo, hi, ramp);
        feats.forEach((f) => extend(bounds, f.geometry));
        any = true;
        this.hover(id, (p) => `<b>${esc(p.name)}</b><br>${fmt(p.value)} ${esc(L.unit || "")}`);
      } else if (L.type === "points") {
        const pts = (L.points || []).filter((p) => Number.isFinite(+p.lat) && Number.isFinite(+p.lon));
        if (!pts.length) continue;
        const vals = pts.map((p) => +p.value).filter(Number.isFinite);
        const hi = vals.length ? Math.max(...vals) : 1;
        this.map.addSource(id, { type: "geojson", data: { type: "FeatureCollection", features: pts.map((p) => ({
          type: "Feature", geometry: { type: "Point", coordinates: [+p.lon, +p.lat] },
          properties: { label: p.label || "", value: Number.isFinite(+p.value) ? +p.value : null,
            detail: p.detail || "", updated: p.updated_at || "" } })) } });
        this.map.addLayer({ id, type: "circle", source: id, paint: {
          "circle-color": "#eb6834", "circle-stroke-color": "#ffffff", "circle-stroke-width": 1.5,
          "circle-radius": vals.length ? ["interpolate", ["linear"], ["coalesce", ["get", "value"], 0], 0, 5, hi || 1, 14] : 7,
          "circle-opacity": 0.9 } });
        this.layers.push(id);
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
        this.map.addLayer({ id, type: "line", source: id, layout: { "line-cap": "round" },
          paint: { "line-color": L.scale === "speed" ? SPEED : "#eb6834", "line-width": 4, "line-opacity": 0.9 } });
        this.layers.push(id);
        lines.forEach((l) => l.coords.forEach((c) => bounds.extend(c)));
        any = true;
        this.hover(id, (p) => `<b>${esc(p.label)}</b><br>${fmt(p.value)} ${esc(L.unit || "")}`);
        if (L.scale === "speed") this.legend(L.label || "Road speed", "mph", 5, 40, ["#d62d20", "#f08a24", "#f2c12e", "#2fae2f"]);
      } else if (L.type === "routes" && (L.routes || []).length) {
        const routes = L.routes.map(String);
        const hit = ["in", ["get", "route"], ["literal", routes]];
        this.map.setPaintProperty("subway", "line-opacity", ["case", hit, 1, 0.12]);
        this.map.setPaintProperty("subway", "line-width", ["case", hit, 5, 1.5]);
        this._routesLit = true;
        g.subway.features.filter((f) => routes.includes(String(f.properties.route)))
          .forEach((f) => extend(bounds, f.geometry));
        any = true;
        this.chip(`${L.label}: ${routes.join(", ")}`);
      } else if (L.type === "focus" && Number.isFinite(+L.lat)) {
        this.map.flyTo({ center: [+L.lon, +L.lat], zoom: L.zoom || 13, speed: 0.9, curve: 1.4 });
        return;
      }
    }
    if (any && !bounds.isEmpty()) {
      this.map.fitBounds(bounds, { padding: 60, maxZoom: 14, duration: 1400 });
    }
  }

  legend(title, unit, lo, hi, ramp) {
    const block = NY.el("div", { class: "lg" },
      NY.el("b", {}, title || "Value"),
      NY.el("div", { class: "bar", style: `background:linear-gradient(90deg,${ramp.join(",")})` }),
      NY.el("div", { class: "ends" }, NY.el("span", {}, `${fmt(lo)} ${unit || ""}`), NY.el("span", {}, `${fmt(hi)} ${unit || ""}`)));
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

function extend(bounds, geom) {
  const walk = (c) => (typeof c[0] === "number" ? bounds.extend(c) : c.forEach(walk));
  walk(geom.coordinates);
}
function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}
function fmt(v) {
  const n = Number(v);
  if (!Number.isFinite(n)) return esc(v);
  return Math.abs(n) >= 100 ? Math.round(n).toLocaleString("en-US") : n.toLocaleString("en-US", { maximumFractionDigits: 1 });
}
