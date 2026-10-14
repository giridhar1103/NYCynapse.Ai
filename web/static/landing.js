// Landing page: the live map drawing, the ticker, examples, the lakehouse diagram, and the
// hand-off to the guide overlay.

import { guide } from "./guide.js?v=10";
import { drawHero } from "./hero.js?v=3";

const { el, get } = NY;
const $ = (id) => document.getElementById(id);

const pulse = get("api/pulse").catch(() => null);
drawHero($("hero-map"), pulse);

// ticker
pulse.then((p) => {
  if (!p) return;
  const items = [];
  if (p.bikes?.stations) items.push([p.bikes.bikes, `Citi Bikes docked at ${p.bikes.stations.toLocaleString("en-US")} stations now`]);
  if (p.subway?.arrivals) items.push([p.subway.arrivals, "train arrivals in the latest 20 minutes of feed data"]);
  if (p.alerts != null) items.push([p.alerts, "subway alerts in effect"]);
  if (p.weather) items.push([`${Math.round(p.weather.temp_f)}°F`, `${p.weather.now} in Manhattan`]);
  if (p.lake?.rows) items.push([`${(p.lake.rows / 1e9).toFixed(2)}B`, "rows of city history in the lake"]);
  $("ticker").replaceChildren(...items.map(([n, label]) => {
    const b = el("b", {}, typeof n === "number" ? "0" : n);
    if (typeof n === "number") countUp(b, n);
    return el("span", { class: "tick" }, b, label);
  }));
  const stats = [
    [p.lake?.rows ? `${(p.lake.rows / 1e9).toFixed(2)}B` : "1B+", "rows across 38 tables"],
    [p.lake ? `${p.lake.fresh_tables}/${p.lake.tables}` : "", "tables on schedule right now"],
    ["7", "city sources, batch and live"],
    ["39", "governed metrics"],
  ];
  $("lake-stats").replaceChildren(...stats.filter((s) => s[0]).map(([n, l]) => el("div", { class: "stat" }, el("b", {}, n), el("span", {}, l))));
});

function countUp(node, target) {
  if (matchMedia("(prefers-reduced-motion: reduce)").matches) { node.textContent = target.toLocaleString("en-US"); return; }
  const t0 = performance.now(), dur = 1400;
  const step = (t) => {
    const k = Math.min(1, (t - t0) / dur), e = 1 - (1 - k) ** 3;
    node.textContent = Math.round(target * e).toLocaleString("en-US");
    if (k < 1) requestAnimationFrame(step);
  };
  requestAnimationFrame(step);
}

// examples and inputs
const heroQ = $("hero-q");
NY.get("api/config").then((cfg) => {
  const ex = cfg.examples || [];
  $("chips").replaceChildren(...ex.slice(0, 6).map((e) => el("button", {
    class: "chip", type: "button", onclick: (ev) => start(e.q, ev.currentTarget) },
    el("span", { class: "tag" + (/live|subway|events/i.test(e.tag) && /now|weekend/i.test(e.q) ? " live" : "") }, e.tag), e.q)));
  typewriter(heroQ, ex.map((e) => e.q));
}).catch(() => {});

function typewriter(input, list) {
  if (!list.length || matchMedia("(prefers-reduced-motion: reduce)").matches) return;
  let i = 0, j = 0, back = false;
  const tick = () => {
    if (document.activeElement === input || input.value) return setTimeout(tick, 1500);
    const s = list[i % list.length];
    input.placeholder = s.slice(0, j);
    if (!back && j < s.length) j += 1;
    else if (!back) { back = true; return setTimeout(tick, 2200); }
    else if (j > 0) j -= 2;
    else { back = false; i += 1; j = 0; }
    setTimeout(tick, back ? 18 : 42);
  };
  setTimeout(tick, 2500);
}

function start(q, origin) {
  guide.open(origin);
  guide.ask(q);
}

for (const [formId, inputId] of [["hero-form", "hero-q"], ["cta-form", "cta-q"]]) {
  $(formId).addEventListener("submit", (e) => {
    e.preventDefault();
    const input = $(inputId);
    const q = input.value.trim() || input.placeholder;
    input.value = "";
    start(q, input);
  });
}
document.querySelectorAll("[data-q]").forEach((n) => n.addEventListener("click", () => start(n.dataset.q, n)));
document.querySelectorAll("[data-open]").forEach((n) => n.addEventListener("click", (e) => {
  e.preventDefault();
  guide.open(n);
}));

const params = new URLSearchParams(location.search);
if (params.get("q")) start(params.get("q"));
else if (location.hash === "#guide") guide.open();

// the lakehouse diagram
function flow() {
  const svg = $("flow");
  const NS = "http://www.w3.org/2000/svg";
  const mk = (tag, attrs, text) => {
    const n = document.createElementNS(NS, tag);
    for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v);
    if (text) n.textContent = text;
    return n;
  };
  const sources = [["TLC trips", "taxis and app rides"], ["MTA subway", "live trains, alerts"], ["Citi Bike", "trips, live docks"],
    ["NYC 311", "service requests"], ["NYPD crashes", "people involved"], ["NYC DOT", "road speeds"], ["Weather", "hourly, alerts"]];
  const stages = [["Ingest", "batch + live pollers", 290], ["DuckLake", "Parquet + Postgres", 470], ["dbt gold", "facts, dimensions", 650],
    ["Semantic layer", "metrics, joins, places", 830]];
  const colors = ["#eda100", "#2a78d6", "#1baf7a", "#eb6834", "#e34948", "#4a3aa7", "#e87ba4"];
  const ys = sources.map((_, i) => 30 + i * 56);
  const midY = 205;
  sources.forEach(([name, sub], i) => {
    const g = mk("g", { class: "node" });
    g.append(mk("rect", { x: 10, y: ys[i], width: 168, height: 44, rx: 10 }),
      mk("circle", { cx: 30, cy: ys[i] + 22, r: 6, fill: colors[i] }),
      mk("text", { x: 46, y: ys[i] + 19 }, name), mk("text", { x: 46, y: ys[i] + 34, class: "sub" }, sub));
    const d = `M178,${ys[i] + 22} C240,${ys[i] + 22} 240,${midY} 300,${midY}`;
    const p = mk("path", { d, class: "pipe", id: `pipe${i}` });
    svg.append(p, g);
    for (let k = 0; k < 2; k++) {
      const c = mk("circle", { class: "spark", r: 3.2, fill: colors[i] });
      const a = mk("animateMotion", { dur: `${2.4 + (i % 3) * 0.5}s`, repeatCount: "indefinite", begin: `${k * 1.3 + i * 0.2}s` });
      a.append(mk("mpath", { href: `#pipe${i}` }));
      c.append(a);
      svg.append(c);
    }
  });
  let prev = null;
  stages.forEach(([name, sub, x], i) => {
    const g = mk("g", { class: "node" });
    g.append(mk("rect", { x, y: midY - 30, width: 150, height: 60, rx: 12 }),
      mk("text", { x: x + 16, y: midY - 4 }, name), mk("text", { x: x + 16, y: midY + 14, class: "sub" }, sub));
    if (prev !== null) {
      const p = mk("path", { d: `M${prev + 150},${midY} L${x},${midY}`, class: "pipe", id: `link${i}` });
      svg.append(p);
      const c = mk("circle", { class: "spark", r: 3.2, fill: "currentColor" });
      const a = mk("animateMotion", { dur: "1.4s", repeatCount: "indefinite", begin: `${i * 0.3}s` });
      a.append(mk("mpath", { href: `#link${i}` }));
      c.append(a);
      svg.append(c);
    }
    svg.append(g);
    prev = x;
  });
  // the guide, fed by the semantic layer and its MCP tools
  const gx = 1046;
  svg.append(mk("path", { d: `M980,${midY} L${gx - 30},${midY}`, class: "pipe" }));
  const guideNode = mk("g", { class: "node" });
  guideNode.append(mk("circle", { cx: gx, cy: midY, r: 30, fill: "#0b0b0b" }),
    mk("text", { x: gx, y: midY + 6, "text-anchor": "middle", fill: "#ffffff", style: "fill:#fff;font-weight:800;font-size:18px" }, "N"),
    mk("text", { x: gx, y: midY + 52, "text-anchor": "middle" }, "Guide"),
    mk("text", { x: gx, y: midY + 68, "text-anchor": "middle", class: "sub" }, "MCP host"));
  svg.append(guideNode);
  const tools = [["Live feeds (MCP)", 330, "#1baf7a"], ["The web (MCP)", 380, "#eb6834"]];
  tools.forEach(([name, y, color], i) => {
    const id = `tool${i}`;
    svg.append(mk("path", { d: `M${gx},${midY + 76} C${gx},${y - 10} ${gx - 60},${y} ${gx - 140},${y}`, class: "pipe", id }));
    const g = mk("g", { class: "node" });
    g.append(mk("rect", { x: gx - 300, y: y - 18, width: 160, height: 36, rx: 9 }), mk("circle", { cx: gx - 282, cy: y, r: 5, fill: color }),
      mk("text", { x: gx - 268, y: y + 5 }, name));
    svg.append(g);
    const c = mk("circle", { class: "spark", r: 3, fill: color });
    const a = mk("animateMotion", { dur: "2.2s", repeatCount: "indefinite", begin: `${i * 0.8}s`, keyPoints: "1;0", keyTimes: "0;1", calcMode: "linear" });
    a.append(mk("mpath", { href: `#${id}` }));
    c.append(a);
    svg.append(c);
  });
}
flow();

// reveal on scroll
const io = new IntersectionObserver((entries) => {
  for (const e of entries) if (e.isIntersecting) { e.target.classList.add("in"); io.unobserve(e.target); }
}, { threshold: 0.12 });
document.querySelectorAll(".reveal").forEach((n) => io.observe(n));
