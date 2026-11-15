// Service worker: keeps the app shell (HTML, JS, CSS, icon) available offline and fast to open.
// API calls (/v1/*, /healthz, /metrics) always go to the network and are never cached: answers,
// prices and chat history must be current.
const VERSION = "dawa-shell-v10";
const SHELL = [
  "/",
  "/index.html",
  "/styles.css",
  "/app.js",
  "/i18n.js",
  "/render.js",
  "/audio.js",
  "/live.js",
  "/recorder-worklet.js",
  "/manifest.webmanifest",
  "/icons/icon.svg",
  "/i18n/interface.js",
  "/i18n/terms.js",
  "/i18n/bn.js",
  "/i18n/gu.js",
  "/i18n/pa.js",
  "/i18n/ta.js",
  "/i18n/te.js",
  "/i18n/kn.js",
  "/i18n/ml.js",
];

self.addEventListener("install", (e) => {
  e.waitUntil(
    caches
      .open(VERSION)
      .then((c) => c.addAll(SHELL))
      .then(() => self.skipWaiting()),
  );
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches
      .keys()
      .then((keys) =>
        Promise.all(
          keys
            .filter(
              (k) => /^(momo-dawa-shell-|dawa-shell-)/.test(k) && k !== VERSION,
            )
            .map((k) => caches.delete(k)),
        ),
      )
      .then(() => self.clients.claim()),
  );
});

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.origin !== location.origin) return;
  if (
    url.pathname.startsWith("/v1/") ||
    url.pathname === "/healthz" ||
    url.pathname === "/metrics"
  )
    return;
  // Network first (new deploys show up at once), cache as the offline fallback.
  e.respondWith(
    fetch(e.request)
      .then((res) => {
        if (res.ok) {
          const copy = res.clone();
          caches.open(VERSION).then((c) => c.put(e.request, copy));
        }
        return res;
      })
      .catch(() =>
        caches
          .match(e.request)
          .then(
            (hit) =>
              hit ||
              (e.request.mode === "navigate"
                ? caches.match("/index.html")
                : Response.error()),
          ),
      ),
  );
});
