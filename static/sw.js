// Minimal service worker so browsers treat the dashboard as an installable app.
// It caches nothing: the dashboard always talks to the live local server.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (e) => e.waitUntil(self.clients.claim()));
self.addEventListener("fetch", () => {});
