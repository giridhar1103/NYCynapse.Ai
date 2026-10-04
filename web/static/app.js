// Shared helpers for every page. No framework; all URLs are relative so the site works under a subpath.
const NY = (() => {
  const ROUTES = {
    rideshare_taxi: ["T", 4], subway: ["S", 1], bike: ["B", 3], city_services: ["3", 2],
    safety: ["X", 8], traffic: ["R", 7], weather: ["W", 5], pipeline_ops: ["D", 6],
    geography: ["P", 0], calendar: ["C", 0],
  };

  function base() {
    // "/nycynapse/evals" -> "/nycynapse/", "/nycynapse/" -> "/nycynapse/"
    const p = location.pathname.replace(/(evals|data|catalog|trace)(\.html)?$/, "");
    return p.endsWith("/") ? p : p + "/";
  }
  const url = (p) => base() + p.replace(/^\//, "");

  async function get(path) {
    const r = await fetch(url(path), { headers: { Accept: "application/json" } });
    if (!r.ok) {
      let msg = r.statusText;
      try { msg = (await r.json()).detail || msg; } catch (_) { /* keep status text */ }
      throw new Error(msg);
    }
    return r.json();
  }

  function el(tag, attrs = {}, ...kids) {
    const n = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (v == null || v === false) continue;
      if (k === "class") n.className = v;
      else if (k === "text") n.textContent = v;
      else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
      else n.setAttribute(k, v === true ? "" : v);
    }
    for (const k of kids.flat(Infinity)) if (k != null) n.append(k.nodeType ? k : document.createTextNode(k));
    return n;
  }

  function bullet(workspace, big) {
    const [letter, color] = ROUTES[workspace] || ["?", 0];
    return el("span", { class: `bullet b${color}`, title: workspace, style: big ? "width:28px;height:28px;font-size:14px" : null }, letter);
  }

  // theme
  function setTheme(t) {
    if (t) document.documentElement.dataset.theme = t; else delete document.documentElement.dataset.theme;
    try { t ? localStorage.setItem("nyc-theme", t) : localStorage.removeItem("nyc-theme"); } catch (_) { /* private mode */ }
  }
  function currentTheme() {
    return document.documentElement.dataset.theme ||
      (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
  }
  try { const t = localStorage.getItem("nyc-theme"); if (t) document.documentElement.dataset.theme = t; } catch (_) { /* ignore */ }

  function header(active) {
    const pages = [["", "Ask"], ["evals", "Evals"], ["data", "Data"], ["catalog", "Catalog"]];
    const toggle = el("button", { class: "theme-toggle", "aria-label": "Switch light or dark",
      onclick: () => { setTheme(currentTheme() === "dark" ? "light" : "dark"); toggle.textContent = icon(); } });
    const icon = () => (currentTheme() === "dark" ? "☀" : "☾");
    toggle.textContent = icon();
    const band = el("header", { class: "band" },
      el("div", { class: "wrap" },
        el("a", { class: "brand", href: url("") }, el("span", { class: "bullet b1" }, "N"), el("span", { class: "word" }, "NYCynapse")),
        el("nav", { class: "nav" }, pages.map(([p, label]) =>
          el("a", { href: url(p), "aria-current": p === active ? "page" : null }, label))),
        toggle));
    document.body.prepend(el("div", {}, band, el("div", { class: "band-rule" })));
    document.body.append(el("footer", {},
      el("div", { class: "wrap" },
        "Questions about New York City, answered from open data with SQL you can read. ",
        el("a", { href: "https://github.com/giridhar1103/NYCynapse.Ai" }, "Agent"), " · ",
        el("a", { href: "https://github.com/giridhar1103/NYCynapse_Lake" }, "Lake"), " · ",
        el("a", { href: "https://giriworks.com" }, "giriworks.com"))));
  }

  // numbers and dates
  const nf = new Intl.NumberFormat("en-US", { maximumFractionDigits: 2 });
  function fmt(v) {
    if (v == null) return "";
    if (typeof v === "number") return Number.isInteger(v) ? v.toLocaleString("en-US") : nf.format(v);
    return String(v);
  }
  function compact(v) {
    if (typeof v !== "number") return String(v);
    const a = Math.abs(v);
    if (a >= 1e9) return (v / 1e9).toFixed(1).replace(/\.0$/, "") + "B";
    if (a >= 1e6) return (v / 1e6).toFixed(1).replace(/\.0$/, "") + "M";
    if (a >= 1e4) return (v / 1e3).toFixed(0) + "k";
    return nf.format(v);
  }
  function ago(iso) {
    const s = (Date.now() - new Date(iso).getTime()) / 1000;
    if (s < 90) return "just now";
    if (s < 5400) return Math.round(s / 60) + " min ago";
    if (s < 129600) return Math.round(s / 3600) + " h ago";
    return Math.round(s / 86400) + " days ago";
  }

  function table(columns, rows, limit = 200) {
    const numeric = columns.map((_, i) => rows.slice(0, 50).every((r) => r[i] == null || typeof r[i] === "number"));
    return el("div", { class: "table-wrap" },
      el("table", { class: "data" },
        el("thead", {}, el("tr", {}, columns.map((c, i) => el("th", { class: numeric[i] ? "num" : null }, c)))),
        el("tbody", {}, rows.slice(0, limit).map((r) =>
          el("tr", {}, r.map((v, i) => el("td", { class: numeric[i] ? "num" : null }, fmt(v))))))));
  }

  // chart: bar or line from the answer's chart spec, first y series only for bars
  let tip;
  function showTip(e, text) {
    if (!tip) { tip = el("div", { class: "tip" }); document.body.append(tip); }
    tip.textContent = text; tip.style.left = e.clientX + "px"; tip.style.top = e.clientY + "px"; tip.hidden = false;
  }
  const hideTip = () => { if (tip) tip.hidden = true; };

  function chart(spec, columns, rows) {
    if (!spec || spec.type === "table" || !rows.length) return null;
    const xi = columns.indexOf(spec.x);
    const yis = (spec.y || []).map((y) => columns.indexOf(y)).filter((i) => i >= 0);
    if (xi < 0 || !yis.length) return null;
    const pts = rows.slice(0, spec.type === "bar" ? 25 : 400).filter((r) => typeof r[yis[0]] === "number");
    if (pts.length < 2) return null;
    const NS = "http://www.w3.org/2000/svg";
    const s = (tag, a = {}) => { const n = document.createElementNS(NS, tag); for (const k in a) n.setAttribute(k, a[k]); return n; };
    const box = el("figure", { class: "chart", style: "margin:0" });
    if (spec.title) box.append(el("figcaption", { class: "hint", style: "margin-bottom:8px" }, spec.title));

    if (spec.type === "bar") {
      const yi = yis[0], rowH = 26, labelW = 170, W = 720, H = pts.length * rowH + 8;
      const max = Math.max(...pts.map((r) => r[yi]), 0) || 1;
      const svg = s("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": spec.title || "bar chart" });
      pts.forEach((r, i) => {
        const y = i * rowH + 4, w = Math.max(2, ((W - labelW - 70) * r[yi]) / max);
        const label = s("text", { x: labelW - 10, y: y + 15, "text-anchor": "end" });
        const name = fmt(r[xi]); label.textContent = name.length > 24 ? name.slice(0, 23) + "…" : name;
        const bar = s("rect", { class: "bar", x: labelW, y: y + 3, width: w, height: rowH - 9, rx: 3 });
        bar.addEventListener("mousemove", (e) => showTip(e, `${name}: ${fmt(r[yi])}`));
        bar.addEventListener("mouseleave", hideTip);
        const v = s("text", { class: "value", x: labelW + w + 6, y: y + 15 }); v.textContent = compact(r[yi]);
        svg.append(label, bar, v);
      });
      box.append(svg);
      return box;
    }

    // line: x is time or ordered labels
    const W = 720, H = 260, L = 56, R = 12, T = 12, B = 32;
    const all = pts.flatMap((r) => yis.map((i) => r[i]).filter((v) => typeof v === "number"));
    let lo = Math.min(...all, 0), hi = Math.max(...all);
    if (hi === lo) hi = lo + 1;
    const X = (i) => L + ((W - L - R) * i) / (pts.length - 1);
    const Y = (v) => T + (H - T - B) * (1 - (v - lo) / (hi - lo));
    const svg = s("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": spec.title || "line chart" });
    for (let k = 0; k <= 4; k++) {
      const v = lo + ((hi - lo) * k) / 4, y = Y(v);
      svg.append(s("line", { class: "grid", x1: L, x2: W - R, y1: y, y2: y }));
      const t = s("text", { x: L - 8, y: y + 4, "text-anchor": "end" }); t.textContent = compact(v); svg.append(t);
    }
    const ticks = Math.min(6, pts.length);
    for (let k = 0; k < ticks; k++) {
      const i = Math.round(((pts.length - 1) * k) / Math.max(1, ticks - 1));
      const t = s("text", { x: X(i), y: H - 10, "text-anchor": k === 0 ? "start" : k === ticks - 1 ? "end" : "middle" });
      t.textContent = String(pts[i][xi]).slice(0, 16); svg.append(t);
    }
    const colors = ["var(--c1)", "var(--c2)", "var(--c3)", "var(--c7)"];
    yis.slice(0, 4).forEach((yi, n) => {
      const d = pts.map((r, i) => `${i ? "L" : "M"}${X(i).toFixed(1)},${Y(r[yi] ?? lo).toFixed(1)}`).join("");
      svg.append(s("path", { class: "series", d, style: `stroke:${colors[n]}` }));
    });
    const hover = s("rect", { x: L, y: T, width: W - L - R, height: H - T - B, fill: "transparent" });
    hover.addEventListener("mousemove", (e) => {
      const b = svg.getBoundingClientRect(), fx = ((e.clientX - b.left) / b.width) * W;
      const i = Math.max(0, Math.min(pts.length - 1, Math.round(((fx - L) / (W - L - R)) * (pts.length - 1))));
      showTip(e, `${pts[i][xi]}: ` + yis.map((yi) => fmt(pts[i][yi])).join(" / "));
    });
    hover.addEventListener("mouseleave", hideTip);
    svg.append(hover);
    box.append(svg);
    if (yis.length > 1) box.append(el("div", { class: "pills", style: "margin-top:8px" },
      yis.slice(0, 4).map((yi, n) => el("span", { class: "pill" }, el("span", { class: "bullet", style: `background:${colors[n]}` }, ""), columns[yi]))));
    return box;
  }

  function tabs(defs) {
    // defs: [[label, node], ...] -> element with a tab row and panels
    const row = el("div", { class: "tabs", role: "tablist" });
    const panels = defs.map(([, node]) => el("div", { class: "panel", role: "tabpanel" }, node));
    defs.forEach(([label], i) => {
      row.append(el("button", { role: "tab", "aria-selected": i === 0 ? "true" : "false", onclick: (e) => {
        row.querySelectorAll("button").forEach((b) => b.setAttribute("aria-selected", "false"));
        e.currentTarget.setAttribute("aria-selected", "true");
        panels.forEach((p, j) => (p.hidden = j !== i));
      } }, label));
      panels[i].hidden = i !== 0;
    });
    return el("div", {}, row, panels);
  }

  const plural = (n, word) => `${fmt(n)} ${word}${n === 1 ? "" : "s"}`;

  return { plural, url, get, el, bullet, header, fmt, compact, ago, table, chart, tabs, ROUTES };
})();
