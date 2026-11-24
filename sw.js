// Monitor Harga Emas v1.7.0 — © 2026 Faiq Aziz
// Service worker: aplikasi tetap bisa dibuka tanpa internet (pakai data terakhir).
const CACHE = "emas-v1.7.0";
const SHELL = ["./", "index.html", "apple-touch-icon.png", "manifest.webmanifest",
  "https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"];

self.addEventListener("install", e => {
  e.waitUntil(caches.open(CACHE).then(c => Promise.all(SHELL.map(u => c.add(u).catch(() => null)))));
  self.skipWaiting();
});
self.addEventListener("activate", e => {
  e.waitUntil(caches.keys().then(ks => Promise.all(ks.filter(k => k !== CACHE).map(k => caches.delete(k)))));
  self.clients.claim();
});
self.addEventListener("fetch", e => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET") return;
  if (url.hostname.includes("gold-api.com")) return;            // harga live: selalu dari internet
  const isData = url.pathname.endsWith("prices.json");
  const isPage = e.request.mode === "navigate" || url.pathname.endsWith("index.html");
  if (isData || isPage) {
    // utamakan internet, simpan salinan; jika offline pakai salinan terakhir
    e.respondWith(fetch(e.request).then(r => {
      const copy = r.clone(); caches.open(CACHE).then(c => c.put(isData ? "prices.json" : "index.html", copy)); return r;
    }).catch(async () => {
      const c = await caches.match(isData ? "prices.json" : "index.html");
      if (!c) return Response.error();
      if (!isData) return c;
      const h = new Headers(c.headers); h.set("x-from-cache", "1");
      return new Response(await c.blob(), { status: 200, headers: h });
    }));
    return;
  }
  e.respondWith(caches.match(e.request).then(c => c || fetch(e.request).then(r => {
    if (r.ok || r.type === "opaque") { const copy = r.clone(); caches.open(CACHE).then(cc => cc.put(e.request, copy)); }
    return r;
  })));
});
