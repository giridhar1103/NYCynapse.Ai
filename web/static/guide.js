// The guide overlay: opens over the landing page, streams one turn at a time from /api/chat,
// draws each tool call as it runs (with errors and recoveries), renders the answer and its
// sources as plain text, and keeps the conversation to a question and three follow-ups.

import { GuideMap, wxIcon } from "./map.js?v=9";

const { el, url } = NY;
const TOOL = {
  ask_lake: ["L", "lake", "City data lake"], lake_freshness: ["L", "lake", "Lake freshness"],
  weather: ["W", "live", "Live weather"], road_speeds: ["R", "live", "Road sensors"],
  road_events: ["R", "live", "511NY road events"], subway_alerts: ["S", "live", "Subway alerts"],
  find_place: ["P", "live", "Place search"], open_data: ["O", "live", "NYC Open Data"],
  area_stats: ["C", "live", "Census by neighborhood"],
  trip_plan: ["T", "live", "Trip planner"], citibike_now: ["B", "live", "Citi Bike now"], transit_changes: ["T", "live", "Train and bus changes"],
  web_search: ["G", "web", "Web search"], read_page: ["R", "web", "Reading a page"],
};
const MODEL_NAMES = { "claude-sonnet-5.5": "Claude Sonnet 5.5", "gemini-3.8-flash": "Gemini 3.8 Flash", "gemini-3.7-flash": "Gemini 3.7 Flash" };

const $ = (id) => document.getElementById(id);

class Guide {
  constructor() {
    this.root = $("guide");
    this.log = $("g-log");
    this.form = $("g-form");
    this.input = $("g-q");
    this.send = $("g-send");
    this.chat = null;
    this.turn = 0;
    this.maxTurns = 4;
    this.busy = false;
    this.config = NY.get("api/config").catch(() => ({ max_turns: 4 }));
    this.map = null;
    this.root.querySelectorAll("[data-close]").forEach((b) => b.addEventListener("click", () => this.close()));
    $("g-new").addEventListener("click", () => this.reset(true));
    this.form.addEventListener("submit", (e) => { e.preventDefault(); this.ask(this.input.value); });
    this.input.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); this.ask(this.input.value); }
    });
    this.input.addEventListener("input", () => {
      this.input.style.height = "auto";
      this.input.style.height = Math.min(120, this.input.scrollHeight) + "px";
    });
    document.addEventListener("keydown", (e) => { if (e.key === "Escape" && this.isOpen()) this.close(); });
    addEventListener("popstate", () => { if (this.isOpen() && location.hash !== "#guide") this.close(true); });
    this.renderTurns();
  }

  isOpen() { return !this.root.hidden; }

  open(origin) {
    if (this.isOpen()) return;
    const r = origin?.getBoundingClientRect?.();
    const panel = this.root.querySelector(".panel");
    if (r) {
      panel.style.setProperty("--ox", `${r.left + r.width / 2}px`);
      panel.style.setProperty("--oy", `${r.top + r.height / 2}px`);
    }
    this.root.hidden = false;
    document.body.classList.add("locked");
    requestAnimationFrame(() => requestAnimationFrame(() => this.root.classList.add("open")));
    if (location.hash !== "#guide") history.pushState(null, "", "#guide");
    if (!this.map) this.map = new GuideMap($("g-map"), $("g-legend"), $("g-chipbar"));
    setTimeout(() => { this.map.resize(); this.input.focus(); }, 480);
  }

  close(fromHistory) {
    this.root.classList.remove("open");
    document.body.classList.remove("locked");
    setTimeout(() => { this.root.hidden = true; }, 380);
    if (!fromHistory && location.hash === "#guide") history.back();
  }

  reset(keepOpen) {
    if (this.busy) return;
    this.chat = null;
    this.turn = 0;
    this.log.replaceChildren();
    this.map?.ready.then(() => this.map.clear());
    this.renderTurns();
    if (keepOpen) this.input.focus();
  }

  renderTurns() {
    const box = $("g-turns");
    box.replaceChildren("Follow-ups ", ...Array.from({ length: this.maxTurns - 1 }, (_, i) =>
      el("i", { class: i < Math.max(0, this.turn - 1) ? "used" : "" })));
    const left = this.turn === 0 ? "A question and up to 3 follow-ups"
      : this.turn >= this.maxTurns ? "That's the last follow-up in this demo. Start a new chat to keep going."
      : `${this.maxTurns - this.turn} follow-up${this.maxTurns - this.turn === 1 ? "" : "s"} left`;
    $("g-left").textContent = left;
    const done = this.turn >= this.maxTurns;
    this.input.disabled = done || this.busy;
    this.send.disabled = done || this.busy;
    this.input.placeholder = done ? "Start a new chat to ask more" : this.turn ? "Ask a follow-up…" : "Ask about New York…";
  }

  async token(view) {
    const cfg = await this.config;
    if (!cfg.turnstile_site_key) return "";
    try { await loadTurnstile(); } catch (_) { return ""; }
    const box = $("ts-box");
    return new Promise((resolve) => {
      let done = false, id = null;
      const finish = (t) => {
        if (done) return;
        done = true;
        resolve(t || "");
        setTimeout(() => { try { if (id != null) window.turnstile.remove(id); } catch (_) { /* gone */ } box.replaceChildren(); }, 50);
      };
      box.replaceChildren();
      id = window.turnstile.render(box, {
        sitekey: cfg.turnstile_site_key, appearance: "interaction-only", size: "flexible",
        retry: "auto", "refresh-expired": "auto",
        callback: finish,
        "before-interactive-callback": () => view.status("Please tick the box below so we know you're human."),
        "error-callback": () => { view.status("Checking you're human…"); return true; },
        "timeout-callback": () => { view.status("The check timed out; please tick the box below."); try { window.turnstile.reset(id); } catch (_) { /* gone */ } },
      });
      setTimeout(() => finish(""), 180000);
    });
  }

  async ask(raw) {
    const q = (raw || "").replace(/\s+/g, " ").trim();
    if (!q || this.busy || this.turn >= this.maxTurns) return;
    if (q.length < 3) return;
    this.open();
    this.input.value = "";
    this.input.style.height = "auto";
    this.busy = true;
    this.renderTurns();
    this.log.append(el("div", { class: "msg-q" }, q));
    const view = new TurnView(this.log);
    this.scroll();
    await this.map?.ready;
    this.map?.clear();
    this.map?.busy(true);
    let token = "";
    if (!this.chat && (await this.config).turnstile_site_key) {
      view.status("Checking you're not a bot");
      token = await this.token(view);
      if (!token) {
        view.error("We couldn't confirm you're human. Please reload the page and try again; if a box appears, tick it.");
        this.busy = false;
        this.map?.busy(false);
        this.renderTurns();
        return;
      }
    }
    const qs = new URLSearchParams({ q, chat: this.chat || "", token });
    try {
      const r = await fetch(url("api/chat?" + qs), { headers: { Accept: "text/event-stream" } });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const reader = r.body.getReader(), dec = new TextDecoder();
      let buf = "", ended = false;
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        let cut;
        while ((cut = buf.indexOf("\n\n")) >= 0) {
          const chunk = buf.slice(0, cut); buf = buf.slice(cut + 2);
          const ev = (chunk.match(/^event: (.*)$/m) || [])[1];
          const data = (chunk.match(/^data: (.*)$/m) || [])[1];
          if (!ev || !data) continue;
          const d = JSON.parse(data);
          if (ev === "chat") { this.chat = d.id; this.maxTurns = d.max_turns || 4; }
          else if (ev === "queue") view.queue(d.position);
          else if (ev === "status") view.status(d.text);
          else if (ev === "tool") view.tool(d);
          else if (ev === "lake") view.lake(d);
          else if (ev === "map") { view.layers.push(...(d.layers || [])); this.map?.show(d.layers); }
          else if (ev === "answer") {
            ended = true;
            this.turn = d.turn;
            view.answer(d, (f) => this.ask(f), (i) => this.map?.focus(i), {
              place: (i) => this.map?.reveal(i), done: () => this.map?.revealAll(), scroll: () => this.scroll(),
              trip: (trip, sel) => this.map?.showTrip(trip, sel) });
            const name = MODEL_NAMES[d.model] || d.model;
            if (name) $("g-model").textContent = name + (d.fell_back ? " (backup)" : "");
          } else if (ev === "error") {
            ended = true;
            view.error(d.message, d.code);
            if (["expired", "full", "unknown_chat"].includes(d.code)) this.chat = null, this.turn = 0;
          }
          this.scroll();
        }
      }
      if (!ended) view.error("The connection closed before the answer arrived. Please try again.");
    } catch (e) {
      view.error("Lost the connection while answering. Please try again.");
    } finally {
      this.busy = false;
      this.map?.busy(false);
      this.renderTurns();
      this.scroll();
      if (this.turn < this.maxTurns) this.input.focus();
    }
  }

  scroll() { this.log.scrollTop = this.log.scrollHeight; }
}


// A trip the way map apps show one: a switch between transit, car, walking and bike with each
// one's time, route cards for transit with the lines in their own colors, and the steps of the
// chosen route. Choosing a mode or a route redraws it on the map.
const MODE_NAME = { transit: "Transit", drive: "Car", walk: "Walk", bike: "Bike" };
const MODE_ICON = {
  transit: '<svg viewBox="0 0 24 24"><path d="M12 2c-4 0-8 .5-8 4v9.5A3.5 3.5 0 0 0 7.5 19L6 20.5v.5h12v-.5L16.5 19a3.5 3.5 0 0 0 3.5-3.5V6c0-3.5-4-4-8-4zM7.5 17a1.5 1.5 0 1 1 0-3 1.5 1.5 0 0 1 0 3zm3.5-7H6V6h5v4zm2 0V6h5v4h-5zm3.5 7a1.5 1.5 0 1 1 0-3 1.5 1.5 0 0 1 0 3z"/></svg>',
  drive: '<svg viewBox="0 0 24 24"><path d="M18.9 6c-.2-.6-.8-1-1.4-1h-11c-.7 0-1.2.4-1.4 1L3 12v8a1 1 0 0 0 1 1h1a1 1 0 0 0 1-1v-1h12v1a1 1 0 0 0 1 1h1a1 1 0 0 0 1-1v-8l-2.1-6zM6.5 16a1.5 1.5 0 1 1 0-3 1.5 1.5 0 0 1 0 3zm11 0a1.5 1.5 0 1 1 0-3 1.5 1.5 0 0 1 0 3zM5 11l1.5-4.5h11L19 11H5z"/></svg>',
  walk: '<svg viewBox="0 0 24 24"><path d="M13.5 5.5a2 2 0 1 0 0-4 2 2 0 0 0 0 4zM9.8 8.9 7 23h2.1l1.8-8 2.1 2v6h2v-7.5l-2.1-2 .6-3A7.3 7.3 0 0 0 19 13v-2a5 5 0 0 1-4.3-2.4l-1-1.6a2 2 0 0 0-1.7-1c-.3 0-.5.1-.8.1L6 8.3V13h2V9.6l1.8-.7"/></svg>',
  bike: '<svg viewBox="0 0 24 24"><path d="M15.5 5.5a2 2 0 1 0 0-4 2 2 0 0 0 0 4zM5 12a5 5 0 1 0 0 10 5 5 0 0 0 0-10zm0 8.5a3.5 3.5 0 1 1 0-7 3.5 3.5 0 0 1 0 7zm5.8-10.3 2.4-2.4.8.8a7 7 0 0 0 5 2.1V8.9a5.3 5.3 0 0 1-3.6-1.5l-1.9-1.9c-.4-.4-1-.6-1.5-.6s-1.1.2-1.4.6L7.8 8.4a2 2 0 0 0 0 2.8L11 14v5h2v-6.2l-2.2-2.6zM19 12a5 5 0 1 0 0 10 5 5 0 0 0 0-10zm0 8.5a3.5 3.5 0 1 1 0-7 3.5 3.5 0 0 1 0 7z"/></svg>',
};

function icon(kind, cls = "ico") {
  const i = el("span", { class: cls, "aria-hidden": "true" });
  i.innerHTML = MODE_ICON[kind] || "";
  return i;
}

function minLabel(m) {
  if (m == null) return "";
  return m >= 60 ? `${Math.floor(m / 60)} h ${m % 60 ? (m % 60) + " min" : ""}`.trim() : `${m} min`;
}

function lineBadge(leg) {
  const bg = leg.color || "#5f6368";
  const fg = leg.text_color || (/^#?(fccc0a|ffd200|ffc72c|fdb827)/i.test(bg) ? "#000" : "#fff");
  const name = leg.name || (leg.mode === "train" ? "Train" : leg.mode === "ferry" ? "Ferry" : "Bus");
  return el("span", { class: `badge ${leg.mode}${name.length <= 2 ? " round" : ""}`, style: `background:${bg};color:${fg}` }, name);
}

function chain(legs) {
  const out = [];
  legs.forEach((g, i) => {
    if (i) out.push(el("span", { class: "sep", "aria-hidden": "true" }, "›"));
    out.push(g.mode === "walk" ? el("span", { class: "walkbit" }, icon("walk", "ico sm"), el("small", {}, String(g.minutes || ""))) : lineBadge(g));
  });
  return el("span", { class: "chain" }, out);
}

// Bus stops and destinations arrive in capitals ("7 AV/W 44 ST"): show them in normal case.
function tidy(name) {
  if (!name) return name;
  const letters = name.replace(/[^A-Za-z]/g, "");
  const caps = letters.replace(/[^A-Z]/g, "").length;
  if (letters.length < 4 || caps / letters.length < 0.7) return name;
  return name.toLowerCase().replace(/(^|[\s/(-])([a-z])/g, (m, a, b) => a + b.toUpperCase())
    .replace(/\b(Av|St|Blvd|Pl|Rd|Pkwy|Ctr|Sq|Hts|Jct)\b/g, (m) => m)
    .replace(/\b(Sbs|Lirr|Jfk|Nyc|Cuny|Ii|Iii)\b/g, (m) => m.toUpperCase())
    .replace(/\b(\d+)(St|Nd|Rd|Th)\b/g, (m, d, x) => d + x.toLowerCase())
    .replace(/\b(Via|And|Of|To|At)\b/g, (m) => m.toLowerCase());
}

function stepList(legs, trip) {
  return el("ol", { class: "steps" }, legs.map((g) => {
    if (g.mode === "walk") {
      return el("li", { class: "walk" }, icon("walk", "ico sm"), el("span", {}, `Walk ${minLabel(g.minutes)}`, ` to ${tidy(g.to || trip.to.name)}`));
    }
    const bits = [g.depart ? `${g.depart} from ${tidy(g.from)}` : g.from ? `from ${tidy(g.from)}` : "",
      g.stops ? `${g.stops} stop${g.stops > 1 ? "s" : ""}` : "", minLabel(g.minutes)].filter(Boolean).join(" · ");
    return el("li", { class: "ride" }, lineBadge(g),
      el("span", {}, el("b", {}, g.headsign ? `toward ${tidy(g.headsign)}` : `to ${tidy(g.to) || ""}`), el("br"),
        el("small", {}, bits, g.live ? el("i", { class: "livedot", title: "Live times" }) : "", g.headsign && g.to ? ` · get off at ${tidy(g.to)}` : "")));
  }));
}

function tripPanel(trip, draw) {
  const modes = Object.keys(trip.modes || {});
  const state = { mode: trip.default || modes[0], i: 0 };
  const box = el("div", { class: "trip" });
  const head = el("div", { class: "trip-head" },
    el("span", { class: "dot start" }), el("b", {}, trip.from.name), el("span", { class: "arrow" }, "→"),
    el("span", { class: "dot end" }), el("b", {}, trip.to.name), el("small", { class: "when" }, trip.when || ""));
  const tabs = el("div", { class: "trip-modes", role: "tablist" });
  const body = el("div", { class: "trip-body" });
  box.append(head, tabs, body);

  const render = () => {
    tabs.replaceChildren(...modes.map((m) => el("button", {
      type: "button", role: "tab", class: "mode" + (m === state.mode ? " on" : ""), "aria-selected": String(m === state.mode),
      onclick: () => { state.mode = m; state.i = 0; render(); draw?.(trip, state); },
    }, icon(m), el("span", {}, el("b", {}, minLabel(trip.modes[m].minutes)), el("small", {}, MODE_NAME[m])))));
    const M = trip.modes[state.mode];
    if (state.mode === "transit") {
      body.replaceChildren(...M.options.map((o, i) => {
        const on = i === state.i;
        const card = el("button", { type: "button", class: "opt" + (on ? " on" : ""),
          onclick: () => { state.i = i; render(); draw?.(trip, state); } },
          el("div", { class: "row1" }, chain(o.legs), el("b", { class: "dur" }, minLabel(o.minutes[0]) + (o.minutes[1] > o.minutes[0] ? "+" : ""))),
          el("div", { class: "row2" }, `${o.leave} – ${o.arrive || ""}`,
            ` · ${o.transfers ? `${o.transfers} change${o.transfers > 1 ? "s" : ""}` : "no changes"}`,
            o.walk_minutes ? ` · ${o.walk_minutes} min walk` : "",
            ...(o.tags || []).map((t) => el("span", { class: "tag" }, t))));
        return on ? el("div", { class: "optwrap" }, card, stepList(o.legs, trip)) : card;
      }));
    } else if (state.mode === "drive") {
      const traffic = M.delay == null ? "" : M.delay >= 2 ? `${M.delay} min slower than usual from traffic` : "Light traffic";
      body.replaceChildren(el("div", { class: "drive" },
        el("div", { class: "big" }, el("b", {}, minLabel(M.minutes)), M.miles ? el("small", {}, ` · ${M.miles} mi`) : ""),
        el("div", { class: "row2" }, M.live ? `${traffic} right now` : M.coords?.length ? "Traffic forecast for that time" : "Typical time"),
        M.typical ? el("div", { class: "row2" }, `Usually ${minLabel(M.typical)} by taxi at this hour`) : "",
        (M.taxi_fare || M.app_fare) ? el("div", { class: "fares" },
          M.taxi_fare ? el("span", {}, "Yellow taxi ", el("b", {}, `$${Math.round(M.taxi_fare)}`)) : "",
          M.app_fare ? el("span", {}, "Uber or Lyft ", el("b", {}, `$${Math.round(M.app_fare)}`)) : "",
          el("small", {}, "typical, with tolls and fees, before tip")) : ""));
    } else {
      body.replaceChildren(el("div", { class: "drive" },
        el("div", { class: "big" }, el("b", {}, minLabel(M.minutes)), el("small", {}, ` · ${M.km} km`)),
        el("div", { class: "row2" }, state.mode === "bike" ? "On bike lanes and quieter streets where possible" : "Walking the whole way")));
    }
  };
  render();
  return box;
}

let uid = 0;

class TurnView {
  constructor(log) {
    this.uid = ++uid;
    this.box = el("div", { class: "msg-a" });
    this.rail = el("div", { class: "rail" });
    this.now = el("div", { class: "now" }, "Reading your question…");
    this.rail.append(this.now);
    this.box.append(this.rail);
    this.steps = {};
    this.layers = [];
    this.started = performance.now();
    log.append(this.box);
  }

  status(text) { this.now.textContent = text; }

  queue(pos) {
    this.now.replaceChildren(el("span", { class: "queue" },
      el("span", { class: "platform" }, el("i"), el("i"), el("i")),
      pos > 0 ? `Busy right now: you're number ${pos} in line. Your question will start shortly.` : "Starting…"));
  }

  tool(d) {
    const [letter, kind, label] = TOOL[d.name] || ["?", d.kind || "live", d.name];
    let s = this.steps[d.id];
    if (!s) {
      s = this.steps[d.id] = {
        row: el("div", { class: `step ${kind} running` }),
        st: el("span", { class: "st" }, ""),
      };
      const what = el("span", { class: "what" }, label, " ", el("small", {}, describe(d.name, d.args)));
      s.row.append(el("span", { class: "ic" }, letter), what, s.st);
      this.rail.insertBefore(s.row, this.now);
    }
    if (d.state === "done" || d.state === "error") {
      s.row.classList.remove("running");
      s.row.classList.add(d.state);
      s.st.append(d.ms != null ? ms(d.ms) : "", d.via === "mcp" ? " · MCP" : "");
      if (d.state === "error" && d.error) {
        const msg = d.error.replace(/[.\s]*$/, ".");
        s.row.append(el("span", { class: "err" }, `${msg[0].toUpperCase()}${msg.slice(1)} The guide will try another way.`));
      }
      if (s.stages) s.stages.querySelectorAll("span").forEach((x) => x.classList.add("on"));
      const sql = (d.sources || []).map((x) => x.sql).find(Boolean);
      if (sql) s.row.append(el("details", { class: "sqlbox" }, el("summary", {}, "SQL"), el("pre", {}, sql)));
    }
  }

  lake(d) {
    const s = this.steps[d.id];
    if (!s) return;
    if (!s.stages) { s.stages = el("div", { class: "stages" }); s.row.append(s.stages); }
    const seen = [...s.stages.children].find((x) => x.dataset.stage === d.stage);
    if (!seen) s.stages.append(el("span", { class: "on", "data-stage": d.stage }, d.label));
    if (d.problem) s.stages.append(el("span", {}, "repairing"));
  }

  answer(d, onFollow, onPlace, cue = {}) {
    const srcs = (d.sources || []).filter((x) => x.title);
    const numbered = Object.fromEntries(srcs.filter((x) => x.n).map((x) => [x.n, x]));
    this.fold(d, srcs.length);
    const card = el("div", { class: "answer-card" + (d.declined ? " declined" : "") });
    card.append(...renderText(d.text || "", { sources: numbered, places: d.places || [], onPlace }));
    const wx = this.layers.find((L) => L.type === "weather" && (L.daily || []).length >= 3);
    if (wx && !d.declined) card.append(forecast(wx.daily));
    const trip = this.layers.filter((L) => L.type === "trip").pop();
    if (trip && !d.declined && Object.keys(trip.modes || {}).length) card.append(tripPanel(trip, cue.trip));
    if (srcs.length && !d.declined) {
      const list = el("ol", { class: "srclist" }, srcs.map((x, i) => {
        const safe = safeUrl(x.url);
        const host = safe ? new URL(safe).hostname.replace(/^www\./, "") : kindName(x.kind);
        const when = x.updated_at ? stamp(x.updated_at) : "";
        const row = [el("span", { class: "n" }, String(x.n || i + 1)),
          el("span", { class: "t" }, x.title),
          el("span", { class: "h" }, [host, when].filter(Boolean).join(" · ")),
          x.stale ? el("span", { class: "stale" }, "old reading") : ""];
        const attrs = { class: `src ${x.kind || "live"}`, title: x.note || x.title, id: x.n ? `s${this.uid}-${x.n}` : null };
        return el("li", {}, safe ? el("a", { ...attrs, href: safe, target: "_blank", rel: "noopener noreferrer nofollow" }, row) : el("span", attrs, row));
      }));
      card.append(el("details", { class: "sources", open: srcs.length <= 3 ? true : null },
        el("summary", {}, `Sources · ${srcs.length}`), list));
      const notes = [...new Set(srcs.filter((x) => x.note || x.attribution).map((x) => x.attribution ? `${x.title}: ${x.attribution}` : x.note))];
      if (notes.length) card.append(el("div", { class: "meta" }, notes.join(" ")));
    }
    if ((d.untraced || []).length) {
      card.append(el("div", { class: "meta warnline" }, `Not matched to the data found: ${d.untraced.join(", ")}`));
    }
    this.box.append(card);
    let follow = null;
    if (d.follow_ups_left > 0 && (d.follow_ups || []).length) {
      follow = el("div", { class: "follow" }, d.follow_ups.map((f) =>
        el("button", { type: "button", onclick: () => onFollow(f) }, f)));
      this.box.append(follow);
    }
    this.stage(card, follow, cue);
  }

  // Lines of the answer appear one after another; as a line naming a place appears, that place
  // lights up on the map. The sources and follow-ups come in last.
  stage(card, follow, cue) {
    const lines = [...card.querySelectorAll(":scope > p, :scope > ul > li")];
    const rest = [...card.children].filter((x) => x.tagName !== "P" && x.tagName !== "UL");
    if (follow) rest.push(follow);
    const all = [...lines, ...rest];
    all.forEach((x) => x.classList.add("rv"));
    const quick = matchMedia("(prefers-reduced-motion: reduce)").matches;
    const step = quick ? 0 : Math.max(110, Math.min(260, 1500 / Math.max(lines.length, 1)));
    lines.forEach((line, n) => setTimeout(() => {
      line.classList.add("on");
      line.querySelectorAll("[data-place]").forEach((b) => cue.place?.(+b.dataset.place));
    }, n * step));
    setTimeout(() => {
      rest.forEach((x) => x.classList.add("on"));
      cue.done?.();
      cue.scroll?.();
    }, lines.length * step + (quick ? 0 : 120));
  }

  // Once the answer is in, the work log folds into one line that opens again on click.
  fold(d, nSources) {
    const steps = Object.keys(this.steps).length;
    const secs = ((d.latency_ms || performance.now() - this.started) / 1000).toFixed(1);
    const bits = d.cached ? ["Stored answer from earlier today"]
      : [`Worked for ${secs} s`, steps ? `${steps} ${steps === 1 ? "check" : "checks"}` : "",
        d.cost_usd ? `$${Number(d.cost_usd).toFixed(3)}` : "", d.fell_back ? "backup model" : ""];
    this.now.textContent = "";
    this.now.hidden = true;
    if (!steps) { this.rail.hidden = true; return; }
    const toggle = el("button", { type: "button", class: "worked", "aria-expanded": "false" },
      bits.filter(Boolean).join(" · "));
    toggle.addEventListener("click", () => {
      const open = this.rail.classList.toggle("folded") === false;
      toggle.setAttribute("aria-expanded", String(open));
    });
    this.rail.classList.add("folded");
    this.box.insertBefore(toggle, this.rail);
  }

  error(message, code) {
    this.now.textContent = "";
    this.box.append(el("div", { class: "notice" + (code === "full" || code === "models" ? "" : " warn") }, message));
  }
}

// Plain text with three small extras: "- " lines become a list, **bold**, citation numbers
// like [2] become source pills, and the places the answer names become buttons that move the map.
function renderText(text, ctx = {}) {
  const out = [];
  let list = null;
  ctx.seen = new Set();
  for (const line of text.split(/\n+/)) {
    const t = line.trim();
    if (!t) continue;
    if (/^[-•*]\s+/.test(t)) {
      if (!list) { list = el("ul"); out.push(list); }
      list.append(el("li", {}, inline(t.replace(/^[-•*]\s+/, ""), ctx)));
    } else {
      list = null;
      out.push(el("p", {}, inline(t, ctx)));
    }
  }
  return out;
}

// A place name that is only part of a longer name is not that place: "Queens" in
// "Brooklyn–Queens Expressway" or "Queensboro", "Brooklyn" in "Brooklyn Bridge".
const LONGER = /^\s(?:Expressway|Expwy|Parkway|Pkwy|Boulevard|Blvd|Bridge|Tunnel|Avenue|Ave|Street|St|Plaza|Center|Mall|College|Hospital|Highway|Hwy|Line|Railroad|Navy Yard|Heights|Museum|Botanic|Public Library|Bus Terminal)\b/;
function insideName(text, start, end) {
  const before = text[start - 1] || " ", after = text[end] || " ";
  if (/[\w'’]/.test(before) || /[\w'’]/.test(after)) return true;
  if (/[–—/]/.test(before) && /\w/.test(text[start - 2] || "")) return true;
  return LONGER.test(text.slice(end, end + 20));
}

function inline(text, ctx, bold = false) {
  const names = (ctx.places || []).map((p, i) => ({ name: p.name, i }))
    .filter((p) => p.name && p.name.length > 2 && !ctx.seen.has(p.i))
    .sort((a, b) => b.name.length - a.name.length);
  const alts = [String.raw`\*\*(.+?)\*\*`, String.raw`((?:\[\d{1,2}\])+)`];
  if (names.length) alts.push(`(${names.map((p) => escRe(p.name)).join("|")})`);
  const re = new RegExp(alts.join("|"), "gi");
  const kids = [];
  let at = 0, m;
  while ((m = re.exec(text))) {
    if (m.index > at) kids.push(text.slice(at, m.index));
    if (m[1] != null && !bold) kids.push(el("strong", {}, inline(m[1], ctx, true)));
    else if (m[1] != null) kids.push(m[1]);
    else if (m[2] != null) kids.push(cite(m[2], ctx.sources || {}));
    else if (m[3] != null) {
      const p = names.find((x) => x.name.toLowerCase() === m[3].toLowerCase());
      if (p && !ctx.seen.has(p.i) && ctx.onPlace && !insideName(text, m.index, re.lastIndex)) {
        ctx.seen.add(p.i);
        // Take in the rest of a hyphenated name: "Spring Creek-Starrett City", "Mott Haven-Port Morris".
        const before = text.slice(at, m.index).match(/(?:[A-Z0-9][\w'’]*(?: [A-Z0-9][\w'’]*)*-)+$/);
        const after = text.slice(re.lastIndex).match(/^(?:-[A-Z0-9][\w'’]*(?: [A-Z0-9][\w'’]*)*)+/);
        const start = before ? m.index - before[0].length : m.index;
        if (before) kids[kids.length - 1] = text.slice(at, start);
        const end = re.lastIndex + (after ? after[0].length : 0);
        kids.push(el("button", { type: "button", class: "place", title: "Show on the map", "data-place": String(p.i), onclick: () => ctx.onPlace(p.i) },
          el("i", {}, String(p.i + 1)), text.slice(start, end)));
        re.lastIndex = end;
      } else kids.push(m[3]);
    }
    at = re.lastIndex;
  }
  if (at < text.length) kids.push(text.slice(at));
  return kids;
}

// "[1][3]" -> one pill naming the first source, "+1" for the rest, linking to the source list.
function cite(raw, sources) {
  const ns = [...raw.matchAll(/\d+/g)].map((x) => +x[0]).filter((n) => sources[n]);
  if (!ns.length) return "";
  const first = sources[ns[0]];
  const safe = safeUrl(first.url);
  // A small number per source, like a footnote; the source names are in the list below the
  // answer and in the tooltip, so the text itself stays clean.
  const tip = ns.map((n) => `${n}. ${sources[n].title}${sources[n].updated_at ? " (" + stamp(sources[n].updated_at) + ")" : ""}`).join("\n");
  const kids = [ns.join(" ")];
  const attrs = { class: `cite ${first.kind || "live"}`, title: tip, "aria-label": `Sources ${ns.join(", ")}: ${shortName(first)}` };
  return safe ? el("a", { ...attrs, href: safe, target: "_blank", rel: "noopener noreferrer nofollow" }, kids)
    : el("span", attrs, kids);
}

const HOSTS = {
  "open-meteo.com": "Open-Meteo", "weather.gov": "NWS", "mta.info": "MTA", "new.mta.info": "MTA",
  "censusreporter.org": "Census", "data.cityofnewyork.us": "NYC Open Data", "geosearch.planninglabs.nyc": "NYC Planning",
  "511ny.org": "511NY", "nyc.gov": "NYC.gov", "forecast.weather.gov": "NWS",
};
function shortName(src) {
  if (src.kind === "lake") return "City data";
  const safe = safeUrl(src.url);
  if (!safe) return kindName(src.kind);
  const host = new URL(safe).hostname.replace(/^www\./, "");
  if (HOSTS[host]) return HOSTS[host];
  const parts = host.split(".");
  return parts.length > 1 ? parts[parts.length - 2] : host;
}
const kindName = (k) => (k === "lake" ? "City data lake" : k === "web" ? "Web" : k === "data" ? "Official data" : "Live feed");

function stamp(v) {
  const s = String(v);
  if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}/.test(s)) return s;
  if (!/[zZ]$|[+-]\d{2}:\d{2}$/.test(s)) {
    // Already New York time: show it as written.
    const [, hh, mm] = s.match(/T(\d{2}):(\d{2})/);
    return `${(+hh % 12) || 12}:${mm} ${+hh < 12 ? "AM" : "PM"}`;
  }
  const d = new Date(s);
  if (Number.isNaN(+d)) return s;
  return d.toLocaleString("en-US", { timeZone: "America/New_York", weekday: "short", hour: "numeric", minute: "2-digit" });
}

function forecast(days) {
  return el("div", { class: "fc" }, days.map((d) => {
    const cell = el("div", { class: "fd" + (d.rain_chance_pct >= 50 ? " wet" : ""), title: `${d.label}: ${d.sky}` });
    const ico = el("span", { class: "ico" });
    ico.innerHTML = wxIcon(d.icon, true);
    cell.append(el("b", {}, d.short), ico,
      el("span", { class: "hi" }, `${d.high_f}°`), el("span", { class: "lo" }, `${d.low_f}°`),
      el("span", { class: "rn" }, `${d.rain_chance_pct}%`));
    return cell;
  }));
}

const escRe = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

function safeUrl(u) {
  try {
    const x = new URL(u);
    return x.protocol === "https:" || x.protocol === "http:" ? x.href : null;
  } catch (_) { return null; }
}

function describe(name, args) {
  if (!args) return "";
  if (name === "ask_lake") return args.question ? `“${String(args.question).slice(0, 70)}”` : "";
  if (name === "weather") return args.days ? `next ${args.days} days` : args.hours ? `next ${args.hours} h` : "";
  if (name === "area_stats") return [String(args.measure || "").replace("_", " "), args.borough].filter(Boolean).join(", ");
  if (name === "subway_alerts") return args.line ? `${args.line} line` : "all lines";
  if (name === "road_speeds" || name === "road_events") return args.borough || "all boroughs";
  if (name === "open_data") return args.dataset || "";
  if (name === "web_search") return args.query ? `“${String(args.query).slice(0, 60)}”` : "";
  if (name === "read_page") { try { return new URL(args.url).hostname; } catch (_) { return ""; } }
  if (name === "find_place") return args.text || "";
  return "";
}

const ms = (v) => (v >= 1000 ? `${(v / 1000).toFixed(1)} s` : `${v} ms`);

let tsLoading = null;
function loadTurnstile() {
  if (window.turnstile) return Promise.resolve();
  if (!tsLoading) {
    tsLoading = new Promise((res, rej) => {
      const s = document.createElement("script");
      s.src = "https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit";
      s.async = true; s.onload = res; s.onerror = rej;
      document.head.append(s);
    });
  }
  return tsLoading;
}

export const guide = new Guide();
