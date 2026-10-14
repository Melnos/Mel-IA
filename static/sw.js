const CACHE = "mel-ia-v3";
const STATIC_ASSETS = ["/", "/app", "/static/atom.js", "/manifest.json"];

self.addEventListener("install", (e) => {
  self.skipWaiting();
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(STATIC_ASSETS)));
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);

  // Never cache API calls
  if (e.request.method !== "GET" || url.pathname.startsWith("/api")) return;

  // Network-first for HTML pages, cache-first for static assets
  if (url.pathname.endsWith(".js") || url.pathname.endsWith(".png") || url.pathname.endsWith(".json")) {
    e.respondWith(
      caches.match(e.request).then((cached) => {
        const fetched = fetch(e.request).then((r) => {
          const cp = r.clone();
          caches.open(CACHE).then((c) => c.put(e.request, cp));
          return r;
        });
        return cached || fetched;
      })
    );
  } else {
    e.respondWith(
      fetch(e.request)
        .then((r) => {
          const cp = r.clone();
          caches.open(CACHE).then((c) => c.put(e.request, cp));
          return r;
        })
        .catch(() => caches.match(e.request))
    );
  }
});
