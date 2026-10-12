// The landing page's drawing of the city: borough outlines and neighborhoods draw themselves,
// subway lines follow in their route colors, then live Citi Bike docks and arriving trains
// appear. Plain SVG; the projection is a simple scaled lon/lat, which is fine at city scale.

const { url } = NY;
const SVGNS = "http://www.w3.org/2000/svg";
const BOX = { w: -74.26, e: -73.69, s: 40.49, n: 40.92 };
const W = 600;
const KX = Math.cos((40.7 * Math.PI) / 180);
const SCALE = W / ((BOX.e - BOX.w) * KX);
const H = (BOX.n - BOX.s) * SCALE;

const px = ([lon, lat]) => [((lon - BOX.w) * KX * SCALE).toFixed(1), ((BOX.n - lat) * SCALE).toFixed(1)];

function pathFor(geom) {
  const rings = geom.type === "Polygon" ? geom.coordinates
    : geom.type === "MultiPolygon" ? geom.coordinates.flat()
    : geom.type === "LineString" ? [geom.coordinates]
    : geom.type === "MultiLineString" ? geom.coordinates : [];
  const closed = geom.type.includes("Polygon");
  return rings.map((r) => "M" + r.map(px).map((p) => p.join(",")).join("L") + (closed ? "Z" : "")).join("");
}

function node(tag, attrs) {
  const n = document.createElementNS(SVGNS, tag);
  for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v);
  return n;
}

const reduce = matchMedia("(prefers-reduced-motion: reduce)").matches;

function draw(el, delay, dur) {
  if (reduce) return;
  const len = el.getTotalLength ? el.getTotalLength() : 0;
  if (!len) return;
  el.style.strokeDasharray = `${len}`;
  el.style.strokeDashoffset = `${len}`;
  el.animate([{ strokeDashoffset: len }, { strokeDashoffset: 0 }],
    { duration: dur, delay, easing: "cubic-bezier(.4,.1,.2,1)", fill: "forwards" });
}

export async function drawHero(container, pulse) {
  const svg = node("svg", { viewBox: `0 0 ${W} ${H.toFixed(0)}`, role: "presentation" });
  container.prepend(svg);
  const gNta = node("g", {}), gBoro = node("g", {}), gSub = node("g", {}), gDots = node("g", {});
  svg.append(gNta, gBoro, gSub, gDots);
  const [boros, subway] = await Promise.all([
    fetch(url("static/geo/boroughs.json")).then((r) => r.json()),
    fetch(url("static/geo/subway.json")).then((r) => r.json()),
  ]);
  boros.features.forEach((f, i) => {
    const p = node("path", { d: pathFor(f.geometry), class: "boro" });
    gBoro.append(p);
    draw(p, i * 220, 2600);
  });
  subway.features.forEach((f, i) => {
    const p = node("path", { d: pathFor(f.geometry), class: "sub", stroke: f.properties.color });
    gSub.append(p);
    draw(p, 1400 + i * 90, 1800);
  });
  // Neighborhood lines are a large file: add them once the main drawing is under way.
  setTimeout(async () => {
    const nta = await fetch(url("static/geo/nta.json")).then((r) => r.json());
    const d = nta.features.map((f) => pathFor(f.geometry)).join("");
    const p = node("path", { d, class: "nta" });
    p.style.opacity = 0;
    gNta.append(p);
    p.animate([{ opacity: 0 }, { opacity: 1 }], { duration: 1600, fill: "forwards" });
  }, 1200);

  const data = await pulse;
  if (!data) return;
  const colors = Object.fromEntries(subway.features.map((f) => [f.properties.route, f.properties.color]));
  (data.bikes?.points || []).forEach(([lon, lat, n], i) => {
    const [x, y] = px([lon, lat]);
    const c = node("circle", { cx: x, cy: y, r: Math.min(2.6, 0.9 + n / 14), class: "bike" });
    c.style.animationDelay = `${3.2 + (i % 60) * 0.03}s`;
    gDots.append(c);
  });
  const trains = (data.subway?.points || []).filter((_, i) => i % 2 === 0).slice(0, 260);
  trains.forEach(([lon, lat, route], i) => {
    const [x, y] = px([lon, lat]);
    const color = colors[route] || "#ee352e";
    const dot = node("circle", { cx: x, cy: y, r: 2.2, class: "train", fill: color });
    dot.style.animationDelay = `${3.6 + (i % 40) * 0.04}s`;
    gDots.append(dot);
    if (i % 4 === 0) {
      const ring = node("circle", { cx: x, cy: y, r: 3, class: "ring", stroke: color });
      ring.style.animationDelay = `${4 + (i % 23) * 0.37}s`;
      gDots.append(ring);
    }
  });
  requestAnimationFrame(() => container.classList.add("ready"));
}
