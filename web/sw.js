// Keeps the site's own files on the device so a second visit opens fast, even on a slow
// connection. Questions and live data (anything under api/) always go to the network.
const CACHE = "nycynapse-v2";

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (e) => {
  e.waitUntil((async () => {
    for (const k of await caches.keys()) if (k !== CACHE) await caches.delete(k);
    await self.clients.claim();
  })());
});

self.addEventListener("fetch", (e) => {
  const req = e.request;
  const url = new URL(req.url);
  if (req.method !== "GET" || url.origin !== location.origin || url.pathname.includes("/api/")) return;
  // Versioned scripts and styles, and the pinned map library, never change at the same address.
  const fixed = url.pathname.includes("/static/") && (url.search || url.pathname.includes("/static/vendor/"));
  if (fixed) {
    e.respondWith(caches.open(CACHE).then(async (c) => {
      const hit = await c.match(req);
      if (hit) return hit;
      const res = await fetch(req);
      if (res.ok) c.put(req, res.clone());
      return res;
    }));
    return;
  }
  // Pages and everything else: the network first, the last copy when offline.
  e.respondWith((async () => {
    const c = await caches.open(CACHE);
    try {
      const res = await fetch(req);
      if (res.ok && (req.mode === "navigate" || url.pathname.includes("/static/"))) c.put(req, res.clone());
      return res;
    } catch (err) {
      const hit = await c.match(req, { ignoreSearch: req.mode === "navigate" });
      if (hit) return hit;
      if (req.mode === "navigate") {
        return new Response("<!doctype html><meta name=viewport content='width=device-width'><body style='font:16px system-ui;background:#0b0b0b;color:#f4f3ee;display:grid;place-items:center;height:100vh;margin:0'><p>You're offline. NYCynapse needs a connection to answer.</p>", { headers: { "Content-Type": "text/html" } });
      }
      throw err;
    }
  })());
});

// Alerts near a watched spot, pushed by the guide's alerts poller.
self.addEventListener("push", (e) => {
  let d = {};
  try { d = e.data ? e.data.json() : {}; } catch (_) { d = { body: e.data && e.data.text() }; }
  e.waitUntil(self.registration.showNotification(d.title || "NYCynapse alert", {
    body: d.body || "Something new near your watched spot.",
    tag: d.tag, icon: "static/icons/icon-192.png", badge: "static/icons/icon-192.png",
    data: { url: d.url || "./#guide" },
  }));
});

self.addEventListener("notificationclick", (e) => {
  e.notification.close();
  const url = new URL((e.notification.data && e.notification.data.url) || "./#guide", self.registration.scope).href;
  e.waitUntil((async () => {
    for (const c of await self.clients.matchAll({ type: "window", includeUncontrolled: true })) {
      if (c.url.startsWith(self.registration.scope)) { await c.focus(); return c.navigate(url); }
    }
    return self.clients.openWindow(url);
  })());
});
