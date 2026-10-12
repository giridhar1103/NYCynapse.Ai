// The guide overlay: opens over the landing page, streams one turn at a time from /api/chat,
// draws each tool call as it runs (with errors and recoveries), renders the answer and its
// sources as plain text, and keeps the conversation to a question and three follow-ups.

import { GuideMap } from "./map.js?v=3";

const { el, url } = NY;
const TOOL = {
  ask_lake: ["L", "lake", "City data lake"], lake_freshness: ["L", "lake", "Lake freshness"],
  weather: ["W", "live", "Live weather"], road_speeds: ["R", "live", "Road sensors"],
  road_events: ["R", "live", "511NY road events"], subway_alerts: ["S", "live", "Subway alerts"],
  find_place: ["P", "live", "Place search"], open_data: ["O", "live", "NYC Open Data"],
  web_search: ["G", "web", "Web search"], read_page: ["R", "web", "Reading a page"],
};
const MODEL_NAMES = { "claude-sonnet-5.5": "Claude Sonnet 5.5", "gemini-3.7-flash": "Gemini 3.7 Flash" };

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
        "timeout-callback": () => finish(""),
      });
      setTimeout(() => finish(""), 90000);
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
    if (this.turn === 0) this.map?.clear();
    this.map?.busy(true);
    let token = "";
    if (!this.chat) {
      view.status("Checking you're not a bot");
      token = await this.token(view);
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
          else if (ev === "map") { this.map?.show(d.layers); }
          else if (ev === "answer") {
            ended = true;
            this.turn = d.turn;
            view.answer(d, (f) => this.ask(f));
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

class TurnView {
  constructor(log) {
    this.box = el("div", { class: "msg-a" });
    this.rail = el("div", { class: "rail" });
    this.now = el("div", { class: "now" }, "Reading your question…");
    this.rail.append(this.now);
    this.box.append(this.rail);
    this.steps = {};
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

  answer(d, onFollow) {
    this.now.textContent = d.cached ? "Answered this morning from the same sources" : "Done";
    const card = el("div", { class: "answer-card" + (d.declined ? " declined" : "") });
    card.append(...renderText(d.text || ""));
    const srcs = (d.sources || []).filter((s) => s.title);
    if (srcs.length) {
      card.append(el("div", { class: "srcs" }, srcs.map((s) => {
        const kind = s.kind || "live";
        const label = (kind === "lake" ? "Lake · " : kind === "web" ? "Web · " : "Live · ") + s.title;
        const attrs = { class: `src ${kind}`, title: s.note || s.title };
        const kids = [el("i"), el("span", {}, label)];
        if (s.stale) kids.push(el("span", { class: "stale" }, " · old reading"));
        if (s.attribution) kids.push(el("span", {}, " · " + s.attribution));
        const safe = safeUrl(s.url);
        return safe ? el("a", { ...attrs, href: safe, target: "_blank", rel: "noopener noreferrer nofollow" }, kids)
          : el("span", attrs, kids);
      })));
      const notes = srcs.filter((s) => s.note).map((s) => s.note);
      if (notes.length) card.append(el("div", { class: "meta" }, [...new Set(notes)].join(" ")));
    }
    if ((d.untraced || []).length) {
      card.append(el("div", { class: "meta warnline" }, `Check: ${d.untraced.join(", ")} could not be matched to the data the guide found.`));
    }
    const secs = ((d.latency_ms || performance.now() - this.started) / 1000).toFixed(1);
    card.append(el("div", { class: "meta" }, d.cached ? "Stored answer from this morning"
      : `${secs} s${d.cost_usd ? ` · about $${Number(d.cost_usd).toFixed(3)} in model calls` : ""}${d.fell_back ? " · answered by the backup model" : ""}`));
    this.box.append(card);
    if (d.follow_ups_left > 0 && (d.follow_ups || []).length) {
      this.box.append(el("div", { class: "follow" }, d.follow_ups.map((f) =>
        el("button", { type: "button", onclick: () => onFollow(f) }, f))));
    }
  }

  error(message, code) {
    this.now.textContent = "";
    this.box.append(el("div", { class: "notice" + (code === "full" || code === "models" ? "" : " warn") }, message));
  }
}

function renderText(text) {
  // Plain text only: lines that start with "- " become a list, the rest paragraphs.
  const out = [];
  let list = null;
  for (const line of text.split(/\n+/)) {
    const t = line.trim();
    if (!t) continue;
    if (/^[-•]\s+/.test(t)) {
      if (!list) { list = el("ul"); out.push(list); }
      list.append(el("li", {}, t.replace(/^[-•]\s+/, "")));
    } else {
      list = null;
      out.push(el("p", {}, t));
    }
  }
  return out;
}

function safeUrl(u) {
  try {
    const x = new URL(u);
    return x.protocol === "https:" || x.protocol === "http:" ? x.href : null;
  } catch (_) { return null; }
}

function describe(name, args) {
  if (!args) return "";
  if (name === "ask_lake") return args.question ? `“${String(args.question).slice(0, 70)}”` : "";
  if (name === "weather") return args.hours ? `next ${args.hours} h` : "";
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
