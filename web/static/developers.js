// The connector page: setup steps per app with the reader's own address filled in, and the tools
// as the server describes them. The address stays in the page.
(() => {
  const { el, get } = NY;
  NY.header("developers");
  const SAMPLE = "https://api.giriworks.com/nycynapse/mcp/YOUR-TOKEN";
  const input = document.getElementById("addr"), note = document.getElementById("addr-note");
  const json = (o) => JSON.stringify(o, null, 2);
  const APPS = [
    ["Claude", (u) => [
      ["On the web or in the desktop app, open Settings, then Connectors, and choose Add custom connector. Name it NYCynapse and paste:", u],
      ["In a chat, open the tools menu and turn NYCynapse on."]]],
    ["Claude Code", (u) => [
      ["Run:", `claude mcp add --transport http nycynapse ${u}`],
      ["Then type /mcp in a session to see that it is connected."]]],
    ["ChatGPT", (u) => [
      ["Custom connectors need developer mode: Settings, Connectors, Advanced. Then choose Create, set authentication to none, and paste:", u],
      ["In a chat, pick NYCynapse from the tools menu. Which plans include this changes, so check your settings."]]],
    ["Cursor", (u) => [
      ["Add this to ~/.cursor/mcp.json, or .cursor/mcp.json in a project:", json({ mcpServers: { nycynapse: { url: u } } })],
      ["Open Settings, MCP and check that it is on."]]],
    ["VS Code", (u) => [
      ["Add this to .vscode/mcp.json:", json({ servers: { nycynapse: { type: "http", url: u } } })],
      ["Run MCP: List Servers from the command palette and start it."]]],
    ["Codex", (u) => [
      ["Run:", `codex mcp add nycynapse --url ${u}`],
      ["Or add this to ~/.codex/config.toml:", `[mcp_servers.nycynapse]\nurl = "${u}"`]]],
    ["Other", (u) => [
      ["Any client that takes a remote server address (streamable HTTP, no authentication):", u],
      ["A client that only starts local programs:", json({ mcpServers: { nycynapse: { command: "npx", args: ["-y", "mcp-remote", u] } } })]]],
  ];
  const valid = (v) => /^https:\/\/api\.giriworks\.com\/nycynapse\/mcp\/[A-Za-z0-9_-]{8,}$/.test(v);
  let current = 0;

  function snippet(text) {
    const b = el("button", { type: "button", class: "copy" }, "Copy");
    b.addEventListener("click", async () => {
      try { await navigator.clipboard.writeText(text); b.textContent = "Copied"; }
      catch (_) { b.textContent = "Select and copy"; }
      setTimeout(() => { b.textContent = "Copy"; }, 1800);
    });
    return el("div", { class: "snip" }, el("pre", {}, el("code", {}, text)), b);
  }
  function show(i, focus) {
    current = i;
    const v = input.value.trim();
    tabs.forEach((t, n) => { t.setAttribute("aria-selected", String(n === i)); t.tabIndex = n === i ? 0 : -1; });
    if (focus) tabs[i].focus();
    document.getElementById("panel").setAttribute("aria-label", APPS[i][0]);
    document.getElementById("panel").replaceChildren(...APPS[i][1](valid(v) ? v : SAMPLE).flatMap(([t, code]) => [el("p", {}, t), code ? snippet(code) : null]).filter(Boolean));
  }
  const tabs = APPS.map(([name], i) => el("button", { type: "button", role: "tab", onclick: () => show(i) }, name));
  const bar = document.getElementById("tabs");
  bar.append(...tabs);
  bar.addEventListener("keydown", (e) => {
    const step = { ArrowRight: 1, ArrowLeft: -1 }[e.key];
    if (step) { e.preventDefault(); show((current + step + tabs.length) % tabs.length, true); }
  });
  input.addEventListener("input", () => {
    const v = input.value.trim();
    note.textContent = !v || valid(v) ? (v ? "Filled in below. It stays on this page and is not sent or saved." : "Paste it to fill in the steps below. It stays on this page and is not sent or saved.")
      : "That does not look like a NYCynapse address. It starts with https://api.giriworks.com/nycynapse/mcp/";
    note.classList.toggle("warn", !!v && !valid(v));
    show(current);
  });
  show(0);

  const GROUPS = [["Getting around", ["trip_plan", "next_trains", "subway_alerts", "transit_changes", "station_access", "citibike_now"]],
    ["Streets and surroundings", ["road_events", "road_speeds", "street_closures", "alerts_near", "nearby_amenities", "find_place"]],
    ["Weather and air", ["weather", "air_quality"]], ["City records", ["open_data"]]];
  get("api/connector-tools").then(({ tools }) => {
    const left = new Map(tools.map((t) => [t.name, t]));
    const group = (name, list) => el("div", { class: "tg" }, el("h3", {}, name), list.map((t) => el("details", {},
      el("summary", {}, el("code", {}, t.name), el("span", {}, t.summary)),
      el("p", {}, t.description),
      el("p", { class: "hint" }, "Takes: ", t.args.length ? t.args.flatMap((a, i) => [i ? ", " : "", el("code", {}, a.name), a.required ? "" : " (optional)"]) : "nothing"))));
    const out = GROUPS.map(([name, names]) => {
      const list = names.filter((n) => left.has(n)).map((n) => { const t = left.get(n); left.delete(n); return t; });
      return list.length ? group(name, list) : null;
    });
    if (left.size) out.push(group("More", [...left.values()]));
    document.getElementById("toollist").replaceChildren(...out.filter(Boolean));
  }).catch((e) => document.getElementById("toollist").replaceChildren(el("p", { class: "hint" }, "The tool list could not be loaded: " + e.message)));
})();
